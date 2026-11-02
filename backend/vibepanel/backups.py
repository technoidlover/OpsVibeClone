"""Sao lưu database bằng công cụ của chính engine (pg_dump, mysqldump, mongodump).

File nằm ở data_dir/backups/<database_id>/<thời điểm>.<đuôi>, đúng định dạng mà import backup
đọc được, nên khôi phục chỉ là import lại file đó. Bật sao lưu tự động thì BackupScheduler tạo
mỗi ngày một bản và giữ `backup_keep` bản gần nhất. Đã cấu hình S3 thì mỗi bản được tải lên
<prefix>/databases/<project>/<database>/ (xem offsite.py), giữ cùng số bản.
Lịch cũng chạy sao lưu dữ liệu service (service_backups.py), sao lưu project (project_backups.py) và
sao lưu panel (panel_backup.py).
"""

import logging
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from . import databases as dbs
from . import offsite
from .config import Settings
from .i18n import t
from .models import Database

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^\d{8}-\d{6}\.(dump|sql\.gz|archive\.gz)$")
CHECK_INTERVAL = 30 * 60
EVERY = timedelta(hours=24)


@dataclass
class BackupInfo:
    name: str
    size: int
    created_at: datetime
    # Trạng thái trên S3: ok | failed | None (chưa cấu hình / chưa tải)
    offsite: str | None = None
    # Còn bản trên đĩa VPS không (False: chỉ còn trên S3)
    local: bool = True


def folder(settings: Settings, database: Database) -> Path:
    return settings.data_dir / "backups" / str(database.id)


def export_cmd(database: Database) -> tuple[list[str], str, bool]:
    """(lệnh ghi bản sao lưu ra stdout, đuôi file, có nén gzip phía panel không)."""
    if database.engine == "postgres":
        # Định dạng custom (đã nén), pg_restore đọc được; import nhận ra qua chữ ký PGDMP
        script = 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc'
        return ["sh", "-c", script], "dump", False
    if database.engine in ("mysql", "mariadb"):
        client, p = ("mysqldump", "MYSQL") if database.engine == "mysql" else ("mariadb-dump", "MARIADB")
        script = (f'MYSQL_PWD="${p}_ROOT_PASSWORD" {client} -h 127.0.0.1 -u root --single-transaction '
                  f'--routines --triggers --no-tablespaces "${p}_DATABASE"')
        return ["sh", "-c", script], "sql.gz", True
    if database.engine == "mongo":
        # Không dùng --gzip của mongodump (nén bên trong archive): nén cả file để import đọc như .archive.gz
        script = ('mongodump --host 127.0.0.1 -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
                  '--authenticationDatabase admin --db "$MONGO_INITDB_DATABASE" --archive --quiet')
        return ["sh", "-c", script], "archive.gz", True
    raise ValueError(t("backup.unsupported", engine=dbs.ENGINES[database.engine].label))


# ---------- Export: tải bản dump về máy, chọn định dạng ----------

EXPORT_FORMATS = {
    "postgres": ("sql.gz", "sql", "dump"),
    "mysql": ("sql.gz", "sql"),
    "mariadb": ("sql.gz", "sql"),
    "mongo": ("archive.gz", "json.zip"),
}
_MONGO_AUTH = ('--host 127.0.0.1 -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
               '--authenticationDatabase admin')


def export_formats(database: Database) -> tuple[str, ...]:
    return EXPORT_FORMATS.get(database.engine, ())


def export(docker, settings: Settings, database: Database, fmt: str) -> Path:
    """Xuất database ra file tạm theo định dạng `fmt` (một trong export_formats). Người gọi xóa file sau khi gửi."""
    if fmt not in export_formats(database):
        raise ValueError(t("backup.bad_format", formats=", ".join(export_formats(database)) or "-"))
    tmp = settings.data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / f"export-{database.id}-{datetime.now(timezone.utc):%Y%m%d%H%M%S%f}.{fmt}"
    try:
        if fmt == "json.zip":
            _export_mongo_json(docker, database, out, tmp)
            return out
        if database.engine == "postgres" and fmt != "dump":
            # SQL thuần, bỏ owner/quyền để nạp được vào server khác
            cmd = ["sh", "-c", 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" '
                               '-d "$POSTGRES_DB" --no-owner --no-privileges']
        else:
            cmd = export_cmd(database)[0]
        docker.exec_to_file(database.container, cmd, out, fmt.endswith(".gz"), dbs.IMPORT_TIMEOUT)
        return out
    except Exception:
        out.unlink(missing_ok=True)
        raise


def _export_mongo_json(docker, database: Database, out: Path, tmp: Path) -> None:
    """Mỗi collection một file JSON (mongoexport --jsonArray), gom vào .zip; import lại được từng file."""
    import zipfile

    code, listing = docker.exec_capture(
        database.container,
        f'mongosh --quiet {_MONGO_AUTH} "$MONGO_INITDB_DATABASE" --eval \'db.getCollectionNames().join("\\n")\'',
        120,
    )
    if code != 0:
        raise RuntimeError(listing.strip()[-2000:])
    names = [n.strip() for n in listing.splitlines() if n.strip() and not n.startswith("system.")]
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for name in names:
            part = tmp / f"{out.name}.{len(zf.namelist())}.json"
            try:
                # Tên collection truyền qua $1, không ghép vào lệnh shell
                docker.exec_to_file(database.container, [
                    "sh", "-c", f'mongoexport {_MONGO_AUTH} --db "$MONGO_INITDB_DATABASE" --collection "$1" '
                                '--jsonArray --quiet', "sh", name,
                ], part, False, dbs.IMPORT_TIMEOUT)
                zf.write(part, f"{name}.json")
            finally:
                part.unlink(missing_ok=True)


def supported(database: Database) -> bool:
    return dbs.ENGINES[database.engine].importable


def create(docker, settings: Settings, database: Database) -> BackupInfo:
    cmd, ext, compress = export_cmd(database)
    target_dir = folder(settings, database)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.{ext}"
    temp = target_dir / f".{name}.part"
    try:
        docker.exec_to_file(database.container, cmd, temp, compress, dbs.IMPORT_TIMEOUT)
        temp.rename(target_dir / name)
    finally:
        temp.unlink(missing_ok=True)
    return info(target_dir / name)


def info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                      offsite.status(path))


def remote_parts(database: Database) -> tuple[str, ...]:
    return ("databases", database.project.name, database.name)


def run(docker, settings: Settings, s3, database: Database) -> BackupInfo:
    """Tạo bản sao lưu, tải lên S3 (nếu có), giữ N bản nếu bật tự động. Người gọi commit DB."""
    created = create(docker, settings, database)
    path = folder(settings, database) / created.name
    offsite.upload(s3, path, *remote_parts(database))
    if database.backup_enabled:
        prune(settings, database, database.backup_keep)
        offsite.prune(s3, database.backup_keep, *remote_parts(database))
    database.last_backup_at = created.created_at
    return info(path)


def list_backups(settings: Settings, database: Database) -> list[BackupInfo]:
    target_dir = folder(settings, database)
    if not target_dir.is_dir():
        return []
    return sorted((info(p) for p in target_dir.iterdir() if NAME_RE.fullmatch(p.name)),
                  key=lambda b: b.name, reverse=True)


def path_of(settings: Settings, database: Database, name: str) -> Path:
    path = folder(settings, database) / name
    if not NAME_RE.fullmatch(name) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete(settings: Settings, database: Database, name: str) -> None:
    path = path_of(settings, database, name)
    path.unlink()
    offsite.forget(path)


def prune(settings: Settings, database: Database, keep: int) -> None:
    for old in list_backups(settings, database)[keep:]:
        path = folder(settings, database) / old.name
        path.unlink(missing_ok=True)
        offsite.forget(path)


def remove_all(settings: Settings, database: Database) -> None:
    shutil.rmtree(folder(settings, database), ignore_errors=True)


def total_size(settings: Settings, database: Database) -> int:
    return sum(b.size for b in list_backups(settings, database))


def due(last: datetime | None, now: datetime) -> bool:
    if last is not None and last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return last is None or now - last >= EVERY


class BackupScheduler:
    """Mỗi 30 phút xem database / service / project / panel nào bật sao lưu tự động mà đã quá 24 giờ chưa có bản mới."""

    def __init__(self, sessions, docker, settings: Settings, box=None):
        self.sessions, self.docker, self.settings, self.box = sessions, docker, settings, box
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[str]:
        from . import panel_backup, project_backups, service_backups
        from .models import Project, Service

        done = []
        now = datetime.now(timezone.utc)
        with self.sessions() as db:
            s3 = offsite.client(db, self.box) if self.box else None
            for database in list(db.scalars(select(Database).where(Database.backup_enabled.is_(True)))):
                if not due(database.last_backup_at, now) or database.import_status == "running" or not supported(database):
                    continue
                try:
                    created = run(self.docker, self.settings, s3, database)
                    db.commit()
                    done.append(f"{database.project.name}/{database.name}: {created.name}")
                except Exception:  # noqa: BLE001 - một database lỗi không chặn các database khác
                    logger.exception("Sao lưu %s/%s thất bại", database.project.name, database.name)
            for service in list(db.scalars(select(Service).where(Service.backup_enabled.is_(True)))):
                if not due(service.last_backup_at, now):
                    continue
                try:
                    created = service_backups.run(self.docker, self.settings, s3, service)
                    db.commit()
                    done.append(f"{service.project.name}/{service.name}: {created.name}")
                except Exception:  # noqa: BLE001
                    logger.exception("Sao lưu dữ liệu %s/%s thất bại", service.project.name, service.name)
            for project in list(db.scalars(select(Project).where(Project.backup_enabled.is_(True)))):
                if not due(project.last_backup_at, now) or project_backups.is_running(project):
                    continue
                try:
                    created = project_backups.run(self.docker, self.settings, s3, project)
                    db.commit()
                    done.append(f"{project.name}: {created.name}")
                except Exception:  # noqa: BLE001
                    logger.exception("Sao lưu project %s thất bại", project.name)
            if self.box is not None:
                try:
                    created = panel_backup.run_if_due(db, self.box, self.docker, self.settings, s3, now)
                    if created:
                        done.append(f"panel: {created.name}")
                except Exception:  # noqa: BLE001
                    logger.exception("Sao lưu panel thất bại")
        for line in done:
            logger.info("Sao lưu: %s", line)
        return done

    def _loop(self) -> None:
        while not self._stop.wait(CHECK_INTERVAL):
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - lịch sao lưu lỗi không được làm sập panel
                logger.exception("Lịch sao lưu thất bại")

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="vp-backups", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
