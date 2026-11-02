"""Database 1-click trong project: tạo, kết nối với service, import backup, xóa."""

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import shutil

from fastapi import APIRouter, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .. import backups, cleanup, offsite
from .. import databases as dbs
from ..config import Settings
from ..crypto import SecretBox
from ..docker import Docker, DockerError
from ..i18n import t
from ..models import Database, Project, Service
from ..runner import JobLogger
from ..validators import Name
from .deps import DB, AppSettings, Box, DockerDep, ProjectDep
from .backup import BackupOut, backup_out, merged
from .jobs import MAX_LOG_CHUNK, JobLogChunk

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects/{project}/databases", tags=["databases"])

# File backup tải lên tối đa 2 GB
MAX_UPLOAD = 2 * 1024 * 1024 * 1024
_import_locks: dict[int, threading.Lock] = {}


class EngineOut(BaseModel):
    key: str
    label: str
    versions: list[str]
    port: int
    importable: bool


class DatabaseCreate(BaseModel):
    name: Name
    engine: Literal["postgres", "mysql", "mariadb", "mongo", "redis"]
    version: str | None = None


class DatabaseOut(BaseModel):
    project: str
    name: str
    engine: str
    engine_label: str
    version: str
    host: str
    port: int
    db_name: str
    username: str
    # starting | running | stopped | failed
    state: str
    importable: bool
    # Định dạng export được (vd sql.gz, sql, dump); rỗng = không export được
    export_formats: list[str] = []
    import_status: str | None
    error: str | None
    # Các service đang dùng database này
    services: list[str]
    created_at: datetime


class Credentials(BaseModel):
    password: str
    url: str
    env: dict[str, str]


class LinkRequest(BaseModel):
    service: str


class LinkResult(BaseModel):
    service: str
    keys: list[str]
    replaced: list[str]
    # Service đang chạy: cần deploy lại để nạp biến môi trường mới
    needs_redeploy: bool


def log_path(settings: Settings, database: Database) -> Path:
    return settings.data_dir / "logs" / "db" / f"{database.id}.log"


def _state(docker: Docker | None, database: Database) -> str:
    if docker is None:
        return "stopped"
    state = docker.container_state(database.container)
    if state is None:
        return "failed" if database.error else "starting"
    if state != "running":
        return "starting" if state == "restarting" else "stopped"
    return "running" if docker.exec_ok(database.container, dbs.health_cmd(database), timeout=10) else "starting"


def database_out(database: Database, docker: Docker | None) -> DatabaseOut:
    engine = dbs.ENGINES[database.engine]
    return DatabaseOut(
        project=database.project.name, name=database.name, engine=database.engine, engine_label=engine.label,
        version=database.version, host=database.host, port=engine.port, db_name=database.db_name,
        username=database.username, state=_state(docker, database), importable=engine.importable,
        export_formats=list(backups.export_formats(database)),
        import_status=database.import_status, error=database.error, created_at=database.created_at,
        services=sorted(s.name for s in database.project.services if database in s.databases),
    )


def _load(db: DB, project: Project, name: str) -> Database:
    obj = db.scalar(select(Database).where(Database.project_id == project.id, Database.name == name))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("database.not_found"))
    return obj


def _need_docker(docker: DockerDep) -> Docker:
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    return docker


# ---------- Việc chạy nền ----------

def _start_container(sessions: sessionmaker[Session], box: SecretBox, docker: Docker, settings: Settings, db_id: int) -> None:
    with sessions() as db:
        database = db.get(Database, db_id)
        if database is None:
            return
        log = JobLogger(log_path(settings, database))
        engine = dbs.ENGINES[database.engine]
        try:
            log.write(t("database.log_starting", label=engine.label, version=database.version, image=dbs.image(database)))
            docker.run_database(
                database.container, dbs.image(database), dbs.container_env(database, box), database.volume,
                engine.data_dir, database.host, {dbs.DB_LABEL: str(database.id)}, dbs.container_cmd(database),
            )
            log.write(t("database.log_waiting", label=engine.label))
            if dbs.wait_ready(docker, database):
                log.write(t("database.log_ready", host=database.host, port=engine.port))
            else:
                log.write(t("database.log_no_response"))
        except DockerError as e:
            database.error = str(e)[:2000]
            db.commit()
            log.write(t("database.log_failed", error=e))
        finally:
            log.close()


def _run_import(
    sessions: sessionmaker[Session], box: SecretBox, docker: Docker, settings: Settings,
    db_id: int, upload: Path, filename: str, clean: bool = False,
) -> None:
    lock = _import_locks.setdefault(db_id, threading.Lock())
    with lock, sessions() as db:
        database = db.get(Database, db_id)
        if database is None:
            upload.unlink(missing_ok=True)
            return
        log = JobLogger(log_path(settings, database))
        for value in dbs.container_env(database, box).values():
            log.add_secret(value)
        try:
            size = upload.stat().st_size / 1024 / 1024
            log.write(t("database.log_import", filename=filename, size=f"{size:.1f}", db_name=database.db_name))
            gz, fmt = dbs.detect_format(upload, filename)
            dbs.check_format(database, fmt)
            collection = f" → collection {dbs.collection_name(filename)}" if fmt.startswith("mongo-json") else ""
            log.write(t("database.log_format", format=f"{fmt}{' (gzip)' if gz else ''}{collection}"))
            if not dbs.wait_ready(docker, database):
                raise RuntimeError(t("database.not_ready"))
            started = time.monotonic()
            docker.exec_stdin(
                database.container, dbs.import_cmd(database, fmt, filename, clean), dbs.read_backup(upload, gz, database.engine),
                log, dbs.IMPORT_TIMEOUT,
            )
            database.import_status, database.error = "success", None
            log.write(t("database.log_done", seconds=f"{time.monotonic() - started:.0f}"))
        except Exception as e:  # noqa: BLE001 - mọi lỗi đều báo cho người dùng
            database.import_status, database.error = "failed", log.redact(str(e))[:2000]
            log.write(t("database.log_failed", error=e))
        finally:
            db.commit()
            log.close()
            upload.unlink(missing_ok=True)


def recover(sessions: sessionmaker[Session]) -> None:
    """Gọi lúc khởi động: import đang chạy dở coi như thất bại."""
    with sessions() as db:
        for database in db.scalars(select(Database).where(Database.import_status == "running")):
            database.import_status, database.error = "failed", t("database.import_interrupted")
        db.commit()


def _spawn(target, *args) -> None:
    threading.Thread(target=target, args=args, daemon=True).start()


# ---------- Dùng chung (cả khi tạo service kèm database) ----------

def new_database(db: Session, project: Project, engine_key: str, version: str | None, name: str, box: SecretBox) -> Database:
    """Thêm bản ghi database (chưa commit, chưa chạy container)."""
    engine = dbs.ENGINES[engine_key]
    version = version or engine.default_version
    if version not in engine.versions:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("database.version_unsupported", label=engine.label, versions=", ".join(engine.versions)))
    if any(s.name == name for s in project.services):
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.service_name_taken", name=name))
    if any(d.name == name for d in project.databases):
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.name_taken_named", name=name))
    database = Database(project=project, name=name, engine=engine_key, version=version, db_name="", username="",
                        password_enc=box.encrypt(dbs.generate_password()))
    database.db_name, database.username = dbs.default_names(database)
    if engine_key in ("mysql", "mariadb"):
        database.root_password_enc = box.encrypt(dbs.generate_password())
    db.add(database)
    return database


def free_name(project: Project, base: str) -> str:
    """Tên chưa dùng trong project: base, base-2, base-3..."""
    taken = {s.name for s in project.services} | {d.name for d in project.databases}
    base = base[:37].strip("-") or "db"
    if base not in taken:
        return base
    return next(f"{base}-{i}" for i in range(2, 1000) if f"{base}-{i}" not in taken)


def link(service: Service, database: Database, box: SecretBox) -> tuple[list[str], list[str]]:
    """Ghi nhận liên kết và thêm biến kết nối vào service; trả (biến đã thêm, biến bị ghi đè)."""
    env = json.loads(box.decrypt(service.env_enc)) if service.env_enc else {}
    added = dbs.link_env(database, box.decrypt(database.password_enc))
    replaced = sorted(k for k in added if k in env and env[k] != added[k])
    env.update(added)
    service.env_enc = box.encrypt(json.dumps(env))
    if database not in service.databases:
        service.databases.append(database)
    return sorted(added), replaced


def start(request: Request, box: SecretBox, docker: Docker, settings: Settings, database: Database) -> None:
    launch(request.app.state.sessions, box, docker, settings, database)


def launch(sessions: sessionmaker[Session], box: SecretBox, docker: Docker, settings: Settings, database: Database) -> None:
    """Chạy container của database vừa tạo. ID database có thể là ID cũ được cấp lại: bỏ log cũ."""
    log_path(settings, database).unlink(missing_ok=True)
    _spawn(_start_container, sessions, box, docker, settings, database.id)


# ---------- API ----------

@router.get("/engines", tags=["databases"])
def list_engines(_project: ProjectDep) -> list[EngineOut]:
    return [EngineOut(key=e.key, label=e.label, versions=list(e.versions), port=e.port, importable=e.importable)
            for e in dbs.ENGINES.values()]


@router.get("")
def list_databases(project: ProjectDep, docker: DockerDep) -> list[DatabaseOut]:
    return [database_out(d, docker) for d in sorted(project.databases, key=lambda d: d.name)]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_database(
    req: DatabaseCreate, project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings,
    request: Request,
) -> DatabaseOut:
    docker = _need_docker(docker)
    database = new_database(db, project, req.engine, req.version, req.name, box)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.name_taken")) from None
    start(request, box, docker, settings, database)
    return database_out(database, None).model_copy(update={"state": "starting"})


@router.get("/{name}")
def get_database(name: str, project: ProjectDep, db: DB, docker: DockerDep) -> DatabaseOut:
    return database_out(_load(db, project, name), docker)


@router.get("/{name}/credentials")
def get_credentials(name: str, project: ProjectDep, db: DB, box: Box) -> Credentials:
    database = _load(db, project, name)
    password = box.decrypt(database.password_enc)
    return Credentials(password=password, url=dbs.connection_url(database, password),
                       env=dbs.link_env(database, password))


@router.post("/{name}/link")
def link_service(name: str, req: LinkRequest, project: ProjectDep, db: DB, box: Box) -> LinkResult:
    """Thêm thông tin kết nối vào biến môi trường của service (deploy lại để có hiệu lực)."""
    database = _load(db, project, name)
    service = db.scalar(select(Service).where(Service.project_id == project.id, Service.name == req.service))
    if service is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("service.not_found"))
    keys, replaced = link(service, database, box)
    db.commit()
    return LinkResult(service=service.name, keys=keys, replaced=replaced, needs_redeploy=service.active_job_id is not None)


@router.delete("/{name}/link/{service}", status_code=status.HTTP_204_NO_CONTENT)
def unlink_service(name: str, service: str, project: ProjectDep, db: DB) -> None:
    """Bỏ liên kết (deploy không chờ database này nữa); biến môi trường giữ nguyên để tự xóa."""
    database = _load(db, project, name)
    obj = db.scalar(select(Service).where(Service.project_id == project.id, Service.name == service))
    if obj is not None and database in obj.databases:
        obj.databases.remove(database)
        db.commit()


@router.post("/{name}/restart", status_code=status.HTTP_202_ACCEPTED)
def restart_database(name: str, project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings,
                     request: Request) -> DatabaseOut:
    docker = _need_docker(docker)
    database = _load(db, project, name)
    try:
        if docker.container_state(database.container) is None:
            # Container bị xóa ngoài panel hoặc tạo lỗi: tạo lại trên volume cũ
            database.error = None
            db.commit()
            _spawn(_start_container, request.app.state.sessions, box, docker, settings, database.id)
        else:
            docker.restart(database.container)
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    return database_out(database, docker)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_database(name: str, project: ProjectDep, db: DB, docker: DockerDep, settings: AppSettings,
                    request: Request, keep_data: bool = False) -> None:
    database = _load(db, project, name)
    if database.import_status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.import_running"))
    remove_runtime(docker, [database], keep_data)
    log_path(settings, database).unlink(missing_ok=True)
    if not keep_data:
        backups.remove_all(settings, database)
    if keep_data:
        cleanup.keep_volumes(db, [database.volume])
    db.delete(database)
    db.commit()
    cleanup.run_in_background(docker, request.app.state.sessions, settings)


def remove_runtime(docker: Docker | None, databases: list[Database], keep_data: bool = False) -> None:
    if docker is None:
        return
    for database in databases:
        docker.remove_container(database.container)
        if not keep_data:
            docker.remove_volume(database.volume)


@router.post("/{name}/import", status_code=status.HTTP_202_ACCEPTED)
async def import_backup(
    name: str, file: UploadFile, project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings,
    request: Request,
) -> DatabaseOut:
    docker = _need_docker(docker)
    database = _load(db, project, name)
    if not dbs.ENGINES[database.engine].importable:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("database.import_unsupported"))
    if database.import_status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.import_busy"))

    uploads = settings.data_dir / "imports"
    uploads.mkdir(parents=True, exist_ok=True)
    target = uploads / f"{database.id}-{int(time.time())}"
    size = 0
    with target.open("wb") as out:
        while chunk := await file.read(dbs.CHUNK):
            size += len(chunk)
            if size > MAX_UPLOAD:
                out.close()
                target.unlink(missing_ok=True)
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, t("database.upload_too_large"))
            out.write(chunk)
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("database.upload_empty"))

    database.import_status, database.error = "running", None
    db.commit()
    filename = Path(file.filename or "backup.sql").name
    _spawn(_run_import, request.app.state.sessions, box, docker, settings, database.id, target, filename)
    return database_out(database, docker)


@router.get("/{name}/logs")
def database_logs(name: str, project: ProjectDep, db: DB, settings: AppSettings, offset: int = Query(0, ge=0)) -> JobLogChunk:
    database = _load(db, project, name)
    path = log_path(settings, database)
    if not path.exists():
        return JobLogChunk(content="", next_offset=0, finished=True)
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(MAX_LOG_CHUNK)
    if len(data) == MAX_LOG_CHUNK and b"\n" in data:
        data = data[: data.rindex(b"\n") + 1]
    return JobLogChunk(content=data.decode("utf-8", errors="replace"), next_offset=offset + len(data),
                       finished=database.import_status != "running")



# ---------- Sao lưu ----------

class BackupsOut(BaseModel):
    supported: bool
    enabled: bool
    keep: int
    last_backup_at: datetime | None
    items: list[BackupOut]


class BackupSettings(BaseModel):
    enabled: bool
    keep: int = Field(7, ge=1, le=60)


def _backups_out(settings: Settings, database: Database, s3) -> BackupsOut:
    return BackupsOut(
        supported=backups.supported(database), enabled=database.backup_enabled, keep=database.backup_keep,
        last_backup_at=database.last_backup_at,
        items=merged(backups.list_backups(settings, database), s3, *backups.remote_parts(database)),
    )


def _backup_path(settings: Settings, database: Database, file: str) -> Path:
    try:
        return backups.path_of(settings, database, file)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.get("/{name}/backups")
def list_backups(name: str, project: ProjectDep, db: DB, box: Box, settings: AppSettings) -> BackupsOut:
    return _backups_out(settings, _load(db, project, name), offsite.client(db, box))


@router.put("/{name}/backups/settings")
def update_backup_settings(req: BackupSettings, name: str, project: ProjectDep, db: DB, box: Box,
                           settings: AppSettings) -> BackupsOut:
    database = _load(db, project, name)
    if req.enabled and not backups.supported(database):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.unsupported", engine=database.engine))
    database.backup_enabled, database.backup_keep = req.enabled, req.keep
    db.commit()
    if req.enabled:
        backups.prune(settings, database, req.keep)
    return _backups_out(settings, database, offsite.client(db, box))


@router.post("/{name}/backups", status_code=status.HTTP_201_CREATED)
def create_backup(name: str, project: ProjectDep, db: DB, box: Box, docker: DockerDep,
                  settings: AppSettings) -> BackupOut:
    """Sao lưu ngay (chạy xong mới trả về; database vài GB có thể mất vài phút)."""
    docker = _need_docker(docker)
    database = _load(db, project, name)
    if not backups.supported(database):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.unsupported", engine=database.engine))
    if database.import_status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.import_busy"))
    try:
        created = backups.run(docker, settings, offsite.client(db, box), database)
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.failed", error=e)) from None
    db.commit()
    return backup_out(created)


@router.get("/{name}/export")
def export_database(name: str, project: ProjectDep, db: DB, docker: DockerDep, settings: AppSettings,
                    format: str = Query(..., max_length=20)) -> FileResponse:
    """Tải bản dump của database về máy (không lưu lại trên VPS như sao lưu)."""
    docker = _need_docker(docker)
    database = _load(db, project, name)
    try:
        out = backups.export(docker, settings, database, format)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except (DockerError, RuntimeError) as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.export_failed", error=e)) from None
    filename = f"{project.name}-{database.name}-{datetime.now():%Y%m%d-%H%M}.{format}"
    return FileResponse(out, filename=filename, media_type="application/octet-stream",
                        background=BackgroundTask(out.unlink, missing_ok=True))


@router.get("/{name}/backups/{file}")
def download_backup(name: str, file: str, project: ProjectDep, db: DB, settings: AppSettings) -> FileResponse:
    database = _load(db, project, name)
    path = _backup_path(settings, database, file)
    return FileResponse(path, filename=f"{project.name}-{database.name}-{file}", media_type="application/octet-stream")


@router.delete("/{name}/backups/{file}", status_code=status.HTTP_204_NO_CONTENT)
def delete_backup(name: str, file: str, project: ProjectDep, db: DB, settings: AppSettings) -> None:
    """Xóa bản trên VPS; bản ở nơi lưu ngoài (nếu có) giữ nguyên."""
    database = _load(db, project, name)
    _backup_path(settings, database, file)
    backups.delete(settings, database, file)


@router.post("/{name}/backups/{file}/restore", status_code=status.HTTP_202_ACCEPTED)
def restore_backup(name: str, file: str, project: ProjectDep, db: DB, box: Box, docker: DockerDep,
                   settings: AppSettings, request: Request) -> DatabaseOut:
    """Khôi phục = import lại file sao lưu (chạy nền như import, xem log của database)."""
    docker = _need_docker(docker)
    database = _load(db, project, name)
    if database.import_status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("database.import_busy"))
    if not backups.NAME_RE.fullmatch(file):
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found"))
    uploads = settings.data_dir / "imports"
    uploads.mkdir(parents=True, exist_ok=True)
    # Import xóa file sau khi nạp xong: làm trên bản sao, bản sao lưu gốc giữ nguyên
    target = uploads / f"{database.id}-{int(time.time())}-restore"
    try:
        shutil.copyfile(backups.path_of(settings, database, file), target)
    except FileNotFoundError:
        # Chỉ còn ở nơi lưu ngoài (vd panel dựng lại trên máy mới): tải về trước
        s3 = offsite.client(db, box)
        if s3 is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None
        try:
            s3.get_file(s3.key(*backups.remote_parts(database), file), target)
        except offsite.ERRORS as e:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.offsite_failed", error=e)) from None
    database.import_status, database.error = "running", None
    db.commit()
    _spawn(_run_import, request.app.state.sessions, box, docker, settings, database.id, target, file, True)
    return database_out(database, docker)
