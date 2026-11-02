"""Sao lưu: nơi lưu ngoài VPS (S3 / Google Drive), sao lưu chính panel, sao lưu dữ liệu (volume) của service,
sao lưu theo project."""

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Literal

import tarfile
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import archive, gdrive, offsite, panel_backup, project_backups, service_backups, vault
from ..backups import BackupInfo
from ..docker import DockerError
from ..i18n import t
from ..models import Job, JobKind, JobStatus, JobTrigger
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep, ProjectDep, Queue, ServiceDep
from .jobs import JobOut

router = APIRouter(tags=["backup"])


class BackupOut(BaseModel):
    name: str
    size: int
    created_at: datetime
    # ok | failed | None: trạng thái ở nơi lưu ngoài
    offsite: str | None = None
    # False: chỉ còn ở nơi lưu ngoài (không có trên đĩa VPS)
    local: bool = True


def backup_out(b: BackupInfo) -> BackupOut:
    return BackupOut(**vars(b))


def merged(local: list[BackupInfo], s3, *parts: str) -> list[BackupOut]:
    """Bản trên VPS + bản chỉ còn ở nơi lưu ngoài (vd panel dựng lại trên máy mới), mới nhất trước."""
    items = {b.name: backup_out(b) for b in local}
    if s3 is not None:
        try:
            for obj in offsite.remote_list(s3, *parts):
                name = obj.key.rsplit("/", 1)[-1]
                if name in items:
                    items[name].offsite = "ok"
                else:
                    items[name] = BackupOut(name=name, size=obj.size, created_at=obj.modified, offsite="ok", local=False)
        except offsite.ERRORS:
            pass  # nơi lưu ngoài tạm lỗi: vẫn hiện bản cục bộ
    return sorted(items.values(), key=lambda b: b.name, reverse=True)


def _remote_error(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.offsite_failed", error=e))


# ---------- Nơi lưu ngoài ----------

class S3Config(BaseModel):
    endpoint: str = Field(min_length=3, max_length=300)
    region: str = Field("auto", max_length=40)
    bucket: str = Field(min_length=3, max_length=63)
    prefix: str = Field("opsvibes", max_length=200)
    access_key: str = Field(min_length=3, max_length=200)
    # Bỏ trống khi sửa: giữ secret key đã lưu
    secret_key: str | None = Field(None, max_length=300)


class DriveStart(BaseModel):
    client_id: str = Field(min_length=10, max_length=300)
    client_secret: str = Field(min_length=5, max_length=300)
    folder: str = Field("OpsVibes", min_length=1, max_length=100)


class DriveStartOut(BaseModel):
    user_code: str
    verification_url: str
    expires_in: int
    interval: int


class PollOut(BaseModel):
    # pending | ok | denied | expired | error
    status: str
    error: str | None = None


class PanelSettings(BaseModel):
    enabled: bool
    keep: int = Field(7, ge=1, le=60)
    # Bỏ trống: giữ passphrase đã lưu
    passphrase: str | None = Field(None, max_length=200)


class PanelOut(BaseModel):
    enabled: bool
    keep: int
    has_passphrase: bool
    last_backup_at: datetime | None
    items: list[BackupOut]


class BackupStatus(BaseModel):
    destination: dict | None
    panel: PanelOut


# Đăng nhập Google đang chờ người dùng nhập mã (mỗi panel một luồng đăng nhập)
_pending_drive: dict = {}
_drive_lock = threading.Lock()


def _panel_out(db, box, settings) -> PanelOut:
    cfg = panel_backup.load(db, box)
    return PanelOut(
        enabled=cfg["enabled"], keep=cfg["keep"], has_passphrase=bool(cfg["passphrase"]),
        last_backup_at=datetime.fromisoformat(cfg["last_backup_at"]) if cfg["last_backup_at"] else None,
        items=merged(panel_backup.list_backups(settings), offsite.client(db, box), *panel_backup.REMOTE),
    )


@router.get("/api/backup")
def backup_status(db: DB, box: Box, settings: AppSettings, _user: CurrentUser) -> BackupStatus:
    return BackupStatus(destination=offsite.public(offsite.load(db, box)), panel=_panel_out(db, box, settings))


@router.put("/api/backup/destination/s3")
def set_s3(req: S3Config, db: DB, box: Box, _user: CurrentUser) -> dict:
    current = offsite.load(db, box)
    secret = req.secret_key or (current["secret_key"] if current and current["type"] == "s3" else None)
    if not secret:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.secret_required"))
    cfg = {"type": "s3", **req.model_dump(exclude={"secret_key"}), "secret_key": secret}
    cfg["prefix"] = cfg["prefix"].strip("/")
    try:
        offsite.client_from(cfg).check()
    except offsite.ERRORS as e:
        raise _remote_error(e) from None
    offsite.save(db, box, cfg)
    db.commit()
    return offsite.public(cfg)


@router.post("/api/backup/destination/gdrive/start")
def gdrive_start(req: DriveStart, _user: CurrentUser) -> DriveStartOut:
    try:
        res = gdrive.device_start(req.client_id)
    except gdrive.DriveError as e:
        raise _remote_error(e) from None
    with _drive_lock:
        _pending_drive.clear()
        _pending_drive.update(req.model_dump(), device_code=res["device_code"],
                              expires=time.monotonic() + int(res.get("expires_in", 1800)))
    return DriveStartOut(user_code=res["user_code"], verification_url=res.get("verification_url") or res.get("verification_uri"),
                         expires_in=int(res.get("expires_in", 1800)), interval=int(res.get("interval", 5)))


@router.post("/api/backup/destination/gdrive/poll")
def gdrive_poll(db: DB, box: Box, _user: CurrentUser) -> PollOut:
    with _drive_lock:
        pending = dict(_pending_drive)
    if not pending or time.monotonic() > pending["expires"]:
        return PollOut(status="expired")
    state, value = gdrive.device_poll(pending["client_id"], pending["client_secret"], pending["device_code"])
    if state in ("pending", "slow_down"):
        return PollOut(status="pending")
    if state != "ok":
        with _drive_lock:
            _pending_drive.clear()
        return PollOut(status=state, error=value)
    cfg = {"type": "gdrive", "client_id": pending["client_id"], "client_secret": pending["client_secret"],
           "refresh_token": value, "folder": pending["folder"]}
    try:
        offsite.client_from(cfg).check()
    except offsite.ERRORS as e:
        return PollOut(status="error", error=str(e))
    with _drive_lock:
        _pending_drive.clear()
    offsite.save(db, box, cfg)
    db.commit()
    return PollOut(status="ok")


@router.post("/api/backup/destination/test", status_code=status.HTTP_204_NO_CONTENT)
def test_destination(db: DB, box: Box, _user: CurrentUser) -> None:
    s3 = offsite.client(db, box)
    if s3 is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.no_destination"))
    try:
        s3.check()
    except offsite.ERRORS as e:
        raise _remote_error(e) from None


@router.delete("/api/backup/destination", status_code=status.HTTP_204_NO_CONTENT)
def remove_destination(db: DB, _user: CurrentUser) -> None:
    """Ngắt kết nối nơi lưu ngoài; các bản đã tải lên vẫn nằm đó."""
    offsite.save(db, None, None)
    db.commit()


# ---------- Sao lưu panel ----------

@router.put("/api/backup/panel")
def set_panel_backup(req: PanelSettings, db: DB, box: Box, settings: AppSettings, _user: CurrentUser) -> PanelOut:
    cfg = panel_backup.load(db, box)
    if req.passphrase:
        if len(req.passphrase) < vault.MIN_PASSPHRASE:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.passphrase_short", min=vault.MIN_PASSPHRASE))
        cfg["passphrase"] = req.passphrase
    if req.enabled and not cfg["passphrase"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.passphrase_required"))
    cfg.update(enabled=req.enabled, keep=req.keep)
    panel_backup.save(db, box, cfg)
    db.commit()
    return _panel_out(db, box, settings)


@router.post("/api/backup/panel", status_code=status.HTTP_201_CREATED)
def create_panel_backup(db: DB, box: Box, docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> BackupOut:
    """Sao lưu toàn bộ ngay (dump mọi database + dữ liệu service; có thể mất vài phút)."""
    if not panel_backup.load(db, box)["passphrase"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("backup.passphrase_required"))
    try:
        return backup_out(panel_backup.run(db, box, docker, settings, offsite.client(db, box)))
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.failed", error=e)) from None


def _download(path: Path, filename: str) -> FileResponse:
    return FileResponse(path, filename=filename, media_type="application/octet-stream")


@router.get("/api/backup/panel/{name}")
def download_panel_backup(name: str, settings: AppSettings, _user: CurrentUser) -> FileResponse:
    try:
        return _download(panel_backup.path_of(settings, name), name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.delete("/api/backup/panel/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_panel_backup(name: str, settings: AppSettings, _user: CurrentUser) -> None:
    try:
        panel_backup.delete(settings, name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


# ---------- Khôi phục toàn bộ ----------

class RestoreRequest(BaseModel):
    passphrase: str = Field(min_length=1, max_length=200)
    # Ghi đè database / dữ liệu đang có trên máy bằng dữ liệu trong bản sao lưu
    overwrite: bool = False


class RestoreState(BaseModel):
    # None: chưa khôi phục lần nào · pending | running | done | failed
    state: str | None
    log: str


def stage_and_restart(request: Request, path: Path, passphrase: str, overwrite: bool) -> dict:
    """Giải mã + kiểm tra bản sao lưu, rồi khởi động lại panel để áp dụng (xem panel_backup.py)."""
    settings = request.app.state.settings
    try:
        manifest = panel_backup.stage_restore(path, passphrase, settings, overwrite)
    except vault.VaultError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except (tarfile.TarError, ValueError, OSError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("restore.bad_file", error=e)) from None
    request.app.state.restart_panel()
    return {"databases": len(manifest.get("databases", [])), "volumes": len(manifest.get("volumes", [])),
            "created_at": manifest.get("created_at")}


async def receive_upload(request: Request, target: Path) -> None:
    """Ghi thân request (nội dung file, không multipart) xuống đĩa theo từng khối."""
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_RESTORE_UPLOAD:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, t("restore.too_large"))
                out.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)
        raise
    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.upload_empty"))


MAX_RESTORE_UPLOAD = 50 * 1024 ** 3


@router.get("/api/backup/restore")
def restore_status(settings: AppSettings, _user: CurrentUser) -> RestoreState:
    return RestoreState(**panel_backup.restore_state(settings))


@router.post("/api/backup/panel/{name}/restore", status_code=status.HTTP_202_ACCEPTED)
def restore_panel_backup(name: str, req: RestoreRequest, request: Request, db: DB, box: Box, settings: AppSettings,
                         _user: CurrentUser) -> dict:
    """Khôi phục từ một bản trong danh sách (trên VPS, hoặc tải về từ nơi lưu ngoài trước)."""
    if not panel_backup.NAME_RE.fullmatch(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found"))
    path = panel_backup.folder(settings) / name
    if not path.is_file():
        s3 = offsite.client(db, box)
        if s3 is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found"))
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            s3.get_file(s3.key(*panel_backup.REMOTE, name), path)
        except offsite.ERRORS as e:
            raise _remote_error(e) from None
    return stage_and_restart(request, path, req.passphrase, req.overwrite)


@router.post("/api/backup/panel/restore-upload", status_code=status.HTTP_202_ACCEPTED)
async def restore_uploaded(request: Request, settings: AppSettings, _user: CurrentUser,
                           x_passphrase: str = Header(..., max_length=200), overwrite: bool = False) -> dict:
    """Tải lên file .vpbak (thân request là nội dung file) rồi khôi phục.

    Passphrase ở header X-Passphrase, mã hóa URL (header chỉ nhận ký tự latin-1, passphrase có thể có dấu)."""
    target = settings.data_dir / "restore-upload.vpbak"
    await receive_upload(request, target)
    try:
        return await run_in_threadpool(stage_and_restart, request, target, unquote(x_passphrase), overwrite)
    finally:
        target.unlink(missing_ok=True)


# ---------- Sao lưu dữ liệu service ----------

class ServiceBackups(BaseModel):
    enabled: bool
    keep: int
    last_backup_at: datetime | None
    # Volume sẽ được sao lưu; rỗng = service không có dữ liệu để sao lưu
    volumes: list[str]
    items: list[BackupOut]


class ServiceBackupSettings(BaseModel):
    enabled: bool
    keep: int = Field(7, ge=1, le=60)


def _service_out(service, db, box, docker, settings) -> ServiceBackups:
    return ServiceBackups(
        enabled=service.backup_enabled, keep=service.backup_keep, last_backup_at=service.last_backup_at,
        volumes=service_backups.volumes(docker, service) if docker is not None else [],
        items=merged(service_backups.list_backups(settings, service), offsite.client(db, box),
                     *service_backups.remote_parts(service)),
    )


SVC = "/api/projects/{project}/services/{service}/backups"


@router.get(SVC)
def list_service_backups(service: ServiceDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings) -> ServiceBackups:
    return _service_out(service, db, box, docker, settings)


@router.put(f"{SVC}/settings")
def set_service_backups(req: ServiceBackupSettings, service: ServiceDep, db: DB, box: Box, docker: DockerDep,
                        settings: AppSettings) -> ServiceBackups:
    service.backup_enabled, service.backup_keep = req.enabled, req.keep
    db.commit()
    if req.enabled:
        service_backups.prune(settings, service, req.keep)
    return _service_out(service, db, box, docker, settings)


@router.post(SVC, status_code=status.HTTP_201_CREATED)
def create_service_backup(service: ServiceDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings) -> BackupOut:
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    try:
        created = service_backups.run(docker, settings, offsite.client(db, box), service)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.failed", error=e)) from None
    db.commit()
    return backup_out(created)


@router.get(f"{SVC}/{{name}}")
def download_service_backup(name: str, service: ServiceDep, settings: AppSettings) -> FileResponse:
    try:
        return _download(service_backups.path_of(settings, service, name), f"{service.project.name}-{service.name}-{name}")
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.delete(f"{SVC}/{{name}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_backup(name: str, service: ServiceDep, settings: AppSettings) -> None:
    try:
        service_backups.delete(settings, service, name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.post(f"{SVC}/{{name}}/restore", status_code=status.HTTP_202_ACCEPTED)
def restore_service_backup(name: str, service: ServiceDep, db: DB, queue: Queue) -> JobOut:
    """Khôi phục chạy như một job (xem log ở tab Deployments): dừng container, ghi đè volume, chạy lại."""
    if not service_backups.NAME_RE.fullmatch(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found"))
    return JobOut.from_job(queue.enqueue(db, service, JobKind.RESTORE, JobTrigger.MANUAL, prompt=name))


# ---------- Sao lưu theo project ----------

class ProjectBackups(BaseModel):
    enabled: bool
    keep: int
    last_backup_at: datetime | None
    # Những gì một bản sẽ gồm: database, service có mã nguồn tải lên / tự viết, service từ GitHub
    databases: list[str]
    sources: list[str]
    git: list[str]
    items: list[BackupOut]
    # Khôi phục gần nhất: running | done | failed | None
    restore_state: str | None
    restore_name: str | None


class ProjectRestoreRequest(BaseModel):
    parts: list[Literal["databases", "source", "data"]] = Field(default_factory=lambda: list(project_backups.PARTS),
                                                                min_length=1)


class ProjectRestoreLog(BaseModel):
    state: str | None
    name: str | None
    log: str


def _project_out(project, db, box, settings) -> ProjectBackups:
    state = project_backups.restore_state(settings, project)
    return ProjectBackups(
        enabled=project.backup_enabled, keep=project.backup_keep, last_backup_at=project.last_backup_at,
        **project_backups.contents(project),
        items=merged(project_backups.list_backups(settings, project), offsite.client(db, box),
                     *project_backups.remote_parts(project)),
        restore_state=state["state"], restore_name=state["name"],
    )


PRJ = "/api/projects/{project}/backups"


@router.get(PRJ)
def list_project_backups(project: ProjectDep, db: DB, box: Box, settings: AppSettings) -> ProjectBackups:
    return _project_out(project, db, box, settings)


@router.put(f"{PRJ}/settings")
def set_project_backups(req: ServiceBackupSettings, project: ProjectDep, db: DB, box: Box,
                        settings: AppSettings) -> ProjectBackups:
    project.backup_enabled, project.backup_keep = req.enabled, req.keep
    db.commit()
    if req.enabled:
        project_backups.prune(settings, project, req.keep)
    return _project_out(project, db, box, settings)


@router.post(PRJ, status_code=status.HTTP_201_CREATED)
def create_project_backup(project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings) -> BackupOut:
    """Sao lưu ngay (chạy xong mới trả về; database / dữ liệu lớn có thể mất vài phút)."""
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    if project_backups.is_running(project):
        raise HTTPException(status.HTTP_409_CONFLICT, t("pbackup.restore_running"))
    try:
        created = project_backups.run(docker, settings, offsite.client(db, box), project)
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, t("backup.failed", error=e)) from None
    db.commit()
    return backup_out(created)


@router.get(f"{PRJ}/restore")
def project_restore_log(project: ProjectDep, settings: AppSettings) -> ProjectRestoreLog:
    return ProjectRestoreLog(**project_backups.restore_state(settings, project))


@router.get(f"{PRJ}/{{name}}")
def download_project_backup(name: str, project: ProjectDep, settings: AppSettings) -> FileResponse:
    try:
        return _download(project_backups.path_of(settings, project, name), f"{project.name}-{name}")
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.get(f"{PRJ}/{{name}}/portable")
def portable_project_backup(name: str, project: ProjectDep, db: DB, box: Box, settings: AppSettings) -> FileResponse:
    """Bản mang đi: .zip thông thường gồm thư mục web và file .sql, dùng được trên hosting khác."""
    try:
        path = project_backups.fetch(db, box, settings, project, name)
        out = project_backups.portable(settings, path)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None
    except offsite.ERRORS as e:
        raise _remote_error(e) from None
    except archive.ArchiveError as e:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e)) from None
    return FileResponse(out, filename=f"{project.name}-{name.removesuffix('.tar')}.zip", media_type="application/zip",
                        background=BackgroundTask(out.unlink, missing_ok=True))


@router.delete(f"{PRJ}/{{name}}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project_backup(name: str, project: ProjectDep, settings: AppSettings) -> None:
    try:
        project_backups.delete(settings, project, name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found")) from None


@router.post(f"{PRJ}/{{name}}/restore", status_code=status.HTTP_202_ACCEPTED)
def restore_project_backup(name: str, req: ProjectRestoreRequest, project: ProjectDep, db: DB, box: Box,
                           docker: DockerDep, settings: AppSettings, queue: Queue, request: Request) -> ProjectRestoreLog:
    """Khôi phục chạy nền; xem tiến trình qua GET .../backups/restore."""
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    if not project_backups.NAME_RE.fullmatch(name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("backup.not_found"))
    if project_backups.is_running(project):
        raise HTTPException(status.HTTP_409_CONFLICT, t("pbackup.restore_running"))
    if any(d.import_status == "running" for d in project.databases):
        raise HTTPException(status.HTTP_409_CONFLICT, t("project.import_running"))
    ids = [s.id for s in project.services]
    if ids and db.scalar(select(Job.id).where(Job.service_id.in_(ids),
                                               Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING])).limit(1)):
        raise HTTPException(status.HTTP_409_CONFLICT, t("project.jobs_running"))
    runner = project_backups.ProjectRestore(request.app.state.sessions, box, docker, settings, queue,
                                            project.id, name, set(req.parts))
    runner.start(project)
    return ProjectRestoreLog(**project_backups.restore_state(settings, project))

