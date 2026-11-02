"""Sao lưu theo project: một file gồm database, mã nguồn tải lên và dữ liệu service của project.

Dành cho người dùng tự quay lại trạng thái cũ khi sửa web bị lỗi (vd WordPress tải lên bằng file
zip), không cần đụng tới sao lưu toàn bộ panel. File nằm ở data_dir/backups/projects/<project_id>/
<thời điểm>.tar, gồm:
- databases/<tên database>.<đuôi>: bản dump (cùng định dạng sao lưu database, xem backups.py);
- sources/<tên service>.bin: file nén mã nguồn (service nguồn archive) đã tải lên;
- files/<tên service>/: file tự viết (Dockerfile, compose.yml...) của service nguồn manual;
- volumes/<tên volume>.tar.gz: volume của service: mã nguồn website PHP đang chạy (site_files.py,
  gồm cả plugin / theme / file sửa trên web) và dữ liệu (ảnh upload...);
- manifest.json.
Service deploy từ GitHub thì mã nguồn đã nằm trên repo nên chỉ có database và dữ liệu.
Cấu hình (biến môi trường, domain...) không nằm trong bản này: khôi phục chỉ quay lại dữ liệu.

Khôi phục chạy nền (ProjectRestore), từng phần chọn được: nạp lại database, mã nguồn (volume mã nguồn,
hoặc file nén / file tự viết rồi deploy lại), dữ liệu (volume khác). Khớp theo tên database / service.
portable() chuyển một bản thành file .zip thông thường (thư mục web + file .sql) để mang sang hosting khác.
"""

import json
import logging
import re
import shutil
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from . import archive, backups, offsite, service_backups, site_files
from .backups import BackupInfo
from .config import Settings
from .crypto import SecretBox
from .i18n import t
from .models import JobKind, JobTrigger, Project, Service
from .runner import JobLogger

logger = logging.getLogger(__name__)

NAME_RE = re.compile(r"^\d{8}-\d{6}\.tar$")
FORMAT = 1
PARTS = ("databases", "source", "data")
_locks: dict[int, threading.Lock] = {}


def folder(settings: Settings, project: Project) -> Path:
    return settings.data_dir / "backups" / "projects" / str(project.id)


def remote_parts(project: Project) -> tuple[str, ...]:
    return ("projects", project.name)


def _info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                      offsite.status(path))


def contents(project: Project) -> dict:
    """Những gì một bản sao lưu của project sẽ gồm (để giao diện giải thích trước khi bấm)."""
    return {
        "databases": sorted(d.name for d in project.databases if backups.supported(d)),
        "sources": sorted(s.name for s in project.services if s.source in ("archive", "manual")),
        "git": sorted(s.name for s in project.services if s.source == "git"),
    }


# ---------- Tạo ----------

def create(docker, settings: Settings, project: Project) -> BackupInfo:
    target = folder(settings, project)
    target.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.tar"
    work = target / f".{name}.d"
    temp = target / f".{name}.part"
    shutil.rmtree(work, ignore_errors=True)
    for sub in ("databases", "sources", "files", "volumes"):
        (work / sub).mkdir(parents=True)
    manifest: dict = {"format": FORMAT, "project": project.name, "created_at": datetime.now(timezone.utc).isoformat(),
                      "databases": [], "skipped_databases": [], "services": []}
    try:
        for database in sorted(project.databases, key=lambda d: d.name):
            if not backups.supported(database):
                continue
            if docker.container_state(database.container) != "running":
                manifest["skipped_databases"].append(database.name)
                continue
            cmd, ext, compress = backups.export_cmd(database)
            file = f"{database.name}.{ext}"
            docker.exec_to_file(database.container, cmd, work / "databases" / file, compress,
                                backups.dbs.IMPORT_TIMEOUT)
            manifest["databases"].append({"name": database.name, "engine": database.engine, "file": file})
        for service in sorted(project.services, key=lambda s: s.name):
            entry: dict = {"name": service.name, "source": service.source, "volumes": []}
            archive = settings.archive_path(service.id)
            if service.source == "archive" and archive.is_file():
                shutil.copyfile(archive, work / "sources" / f"{service.name}.bin")
                entry["archive"] = f"{service.name}.bin"
                entry["archive_name"] = service.archive_name
            files = settings.files_dir(service.id)
            if service.source == "manual" and files.is_dir():
                shutil.copytree(files, work / "files" / service.name, symlinks=True)
                entry["files"] = True
            for volume, mount in service_backups.mounts(docker, service).items():
                docker.volume_export(volume, work / "volumes" / f"{volume}.tar.gz", service_backups.TIMEOUT)
                entry["volumes"].append({"volume": volume, "mount": mount,
                                         "site": site_files.is_site_volume(service, volume)})
            if entry["volumes"] or entry.get("archive") or entry.get("files"):
                manifest["services"].append(entry)
        if not manifest["databases"] and not manifest["services"]:
            raise ValueError(t("pbackup.nothing"))
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        with tarfile.open(temp, "w") as tf:
            for item in ("manifest.json", "databases", "sources", "files", "volumes"):
                tf.add(work / item, arcname=item)
        temp.rename(target / name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        temp.unlink(missing_ok=True)
    return _info(target / name)


def run(docker, settings: Settings, s3, project: Project) -> BackupInfo:
    """Tạo bản sao lưu, tải lên nơi lưu ngoài (nếu có), giữ N bản nếu bật tự động. Người gọi commit DB."""
    created = create(docker, settings, project)
    path = folder(settings, project) / created.name
    offsite.upload(s3, path, *remote_parts(project))
    if project.backup_enabled:
        prune(settings, project, project.backup_keep)
        offsite.prune(s3, project.backup_keep, *remote_parts(project))
    project.last_backup_at = created.created_at
    return _info(path)


def list_backups(settings: Settings, project: Project) -> list[BackupInfo]:
    target = folder(settings, project)
    if not target.is_dir():
        return []
    return sorted((_info(p) for p in target.iterdir() if NAME_RE.fullmatch(p.name)),
                  key=lambda b: b.name, reverse=True)


def path_of(settings: Settings, project: Project, name: str) -> Path:
    path = folder(settings, project) / name
    if not NAME_RE.fullmatch(name) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete(settings: Settings, project: Project, name: str) -> None:
    path = path_of(settings, project, name)
    path.unlink()
    offsite.forget(path)


def prune(settings: Settings, project: Project, keep: int) -> None:
    for old in list_backups(settings, project)[keep:]:
        path = folder(settings, project) / old.name
        path.unlink(missing_ok=True)
        offsite.forget(path)


def remove_all(settings: Settings, project: Project) -> None:
    shutil.rmtree(folder(settings, project), ignore_errors=True)
    for path in (state_path(settings, project), log_path(settings, project)):
        path.unlink(missing_ok=True)


def total_size(settings: Settings, project: Project) -> int:
    return sum(b.size for b in list_backups(settings, project))


# ---------- Khôi phục ----------

def fetch(db: Session, box: SecretBox, settings: Settings, project: Project, name: str,
          log: JobLogger | None = None) -> Path:
    """Đường dẫn bản sao lưu trên VPS; chỉ còn ở nơi lưu ngoài thì tải về trước (giữ lại như bản thường)."""
    if not NAME_RE.fullmatch(name):
        raise FileNotFoundError(name)
    path = folder(settings, project) / name
    if path.is_file():
        return path
    s3 = offsite.client(db, box)
    if s3 is None:
        raise FileNotFoundError(name)
    if log is not None:
        log.write(t("backup.downloading", name=name))
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{name}.part")
    try:
        s3.get_file(s3.key(*remote_parts(project), name), temp)
        temp.rename(path)
    finally:
        temp.unlink(missing_ok=True)
    path.with_name(path.name + ".s3").write_text("")
    return path



def log_path(settings: Settings, project: Project) -> Path:
    return settings.data_dir / "logs" / "projects" / f"{project.id}-restore.log"


def state_path(settings: Settings, project: Project) -> Path:
    return settings.data_dir / "logs" / "projects" / f"{project.id}-restore.json"


def restore_state(settings: Settings, project: Project) -> dict:
    path = state_path(settings, project)
    state = json.loads(path.read_text()) if path.is_file() else {}
    log = log_path(settings, project)
    return {"state": state.get("state"), "name": state.get("name"),
            "log": log.read_text(errors="replace")[-20000:] if log.is_file() else ""}


def is_running(project: Project) -> bool:
    lock = _locks.get(project.id)
    return bool(lock and lock.locked())


def recover(settings: Settings) -> None:
    """Gọi lúc khởi động: khôi phục đang chạy dở (panel khởi động lại giữa chừng) coi như thất bại."""
    base = settings.data_dir / "logs" / "projects"
    for path in base.glob("*-restore.json") if base.is_dir() else []:
        state = json.loads(path.read_text())
        if state.get("state") == "running":
            path.write_text(json.dumps({**state, "state": "failed"}))


class ProjectRestore:
    """Khôi phục một bản sao lưu của project (chạy nền, log ở log_path)."""

    def __init__(self, sessions: sessionmaker[Session], box: SecretBox, docker, settings: Settings, queue,
                 project_id: int, name: str, parts: set[str]):
        self.sessions, self.box, self.docker, self.settings, self.queue = sessions, box, docker, settings, queue
        self.project_id, self.name, self.parts = project_id, name, parts

    def start(self, project: Project) -> None:
        """Ghi trạng thái rồi chạy nền. Người gọi đã kiểm tra is_running()."""
        log_path(self.settings, project).parent.mkdir(parents=True, exist_ok=True)
        log_path(self.settings, project).unlink(missing_ok=True)
        self._set_state(project, "running")
        lock = _locks.setdefault(project.id, threading.Lock())
        lock.acquire()

        def target():
            try:
                self.run()
            finally:
                lock.release()

        threading.Thread(target=target, name=f"vp-project-restore-{project.id}", daemon=True).start()

    def _set_state(self, project: Project, state: str) -> None:
        state_path(self.settings, project).write_text(json.dumps({"state": state, "name": self.name}))

    def run(self) -> None:
        with self.sessions() as db:
            project = db.get(Project, self.project_id)
            if project is None:
                return
            log = JobLogger(log_path(self.settings, project))
            work = self.settings.data_dir / "tmp" / f"project-restore-{project.id}"
            try:
                self._run(db, project, log, work)
                self._set_state(project, "done")
                log.write(t("pbackup.done"))
            except Exception as e:  # noqa: BLE001 - lỗi nào cũng phải hiện cho người dùng
                logger.exception("Khôi phục project %s thất bại", project.name)
                self._set_state(project, "failed")
                log.write(t("pbackup.failed", error=log.redact(str(e))))
            finally:
                shutil.rmtree(work, ignore_errors=True)
                log.close()

    def _run(self, db: Session, project: Project, log: JobLogger, work: Path) -> None:
        from .api.databases import _run_import

        docker = self.docker
        path = fetch(db, self.box, self.settings, project, self.name, log)
        log.write(t("pbackup.start", name=self.name))
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        with tarfile.open(path) as tf:
            if hasattr(tarfile, "data_filter"):
                tf.extractall(work, filter="data")
            else:
                tf.extractall(work)
        manifest = json.loads((work / "manifest.json").read_text())

        # 1. Database: nạp lại dump (thay bảng cũ bằng bảng trong bản sao lưu)
        if "databases" in self.parts:
            by_name = {d.name: d for d in project.databases}
            for entry in manifest.get("databases", []):
                database = by_name.get(entry["name"])
                if database is None or database.engine != entry["engine"]:
                    log.write(t("pbackup.db_missing", name=entry["name"]))
                    continue
                log.write(t("pbackup.db_import", name=database.name))
                dump = self.settings.data_dir / "imports" / f"{database.id}-project-restore"
                dump.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(work / "databases" / entry["file"], dump)
                database.import_status, database.error = "running", None
                db.commit()
                _run_import(self.sessions, self.box, docker, self.settings, database.id, dump, entry["file"], clean=True)
                db.refresh(database)
                if database.import_status != "success":
                    raise RuntimeError(t("pbackup.db_failed", name=database.name, error=database.error or ""))
                log.write(t("pbackup.db_done", name=database.name))
            for name in manifest.get("skipped_databases", []):
                log.write(t("pbackup.db_skipped", name=name))

        # 2. Service: mã nguồn (volume mã nguồn / file nén / file tự viết) và dữ liệu (volume khác)
        by_service = {s.name: s for s in project.services}
        for entry in manifest.get("services", []):
            service = by_service.get(entry["name"])
            if service is None:
                log.write(t("pbackup.service_missing", name=entry["name"]))
                continue
            redeploy = False
            pairs: list[tuple[str, Path]] = []
            for vol in entry["volumes"]:
                file = work / "volumes" / f"{vol['volume']}.tar.gz"
                if vol["site"] and "source" in self.parts:
                    if not service.site_volume:
                        # Service chưa từng chạy bằng volume mã nguồn: tạo volume rồi deploy lại để gắn vào
                        service.site_volume = site_files.new_name(service, f"r{int(time.time())}")
                        redeploy = True
                    site_files.fresh_marker(self.settings, service).unlink(missing_ok=True)
                    pairs.append((service.site_volume, file))
                elif not vol["site"] and "data" in self.parts:
                    volume = service_backups.target(service, vol["volume"])
                    if volume:
                        pairs.append((volume, file))
            has_site = any(v["site"] for v in entry["volumes"])
            if "source" in self.parts and service.source == entry["source"]:
                # Có volume mã nguồn thì file nén chỉ là bản gốc đã tải lên: đặt lại, không cần deploy
                restored = self._restore_source(service, entry, work, log)
                redeploy = redeploy or (restored and not has_site)
            db.commit()
            if pairs:
                self._restore_volumes(service, pairs, log)
            if redeploy:
                log.write(t("pbackup.deploy", name=service.name))
                self.queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.MANUAL)

    def _restore_source(self, service: Service, entry: dict, work: Path, log: JobLogger) -> bool:
        if entry.get("archive"):
            target = self.settings.archive_path(service.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_name(f"{service.id}-restore.upload")
            shutil.move(work / "sources" / entry["archive"], temp)
            # Job đang dùng file cũ vẫn đọc được inode cũ (giống tải lên bản mới)
            temp.replace(target)
            service.archive_name = entry.get("archive_name") or service.archive_name
            log.write(t("pbackup.source", name=service.name, file=service.archive_name or "-"))
            return True
        if entry.get("files"):
            target = self.settings.files_dir(service.id)
            shutil.rmtree(target, ignore_errors=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(work / "files" / service.name, target)
            log.write(t("pbackup.files", name=service.name))
            return True
        return False

    def _restore_volumes(self, service: Service, pairs: list[tuple[str, Path]], log: JobLogger) -> None:
        docker = self.docker
        containers = [c for c in docker.containers_for_service(service.id) if docker.container_state(c) == "running"]
        for container in containers:
            log.write(t("backup.stop", container=container))
            docker.stop(container)
        try:
            for volume, file in pairs:
                log.write(t("backup.restore_volume", volume=volume, size=f"{file.stat().st_size / 1024 / 1024:.1f}"))
                with file.open("rb") as f:
                    docker.volume_import(volume, f, service_backups.TIMEOUT)
        finally:
            for container in containers:
                log.write(t("backup.start", container=container))
                try:
                    docker.start(container)
                except Exception as e:  # noqa: BLE001 - chạy lại lỗi vẫn báo trong log, không che lỗi khôi phục
                    log.write(t("deploy.restart_old_failed", name=container, error=e))


# ---------- Bản mang đi (sang hosting khác) ----------

def _relative(mount: str | None, base: str) -> str | None:
    if mount and mount.startswith(base + "/"):
        return mount[len(base) + 1:].strip("/")
    return None


def _zip_tree(zf: zipfile.ZipFile, src, prefix: str) -> None:
    """Chép nội dung một file tar(.gz) (đọc tuần tự từ `src`) vào zip dưới thư mục `prefix`."""
    with tarfile.open(fileobj=src, mode="r|*") as tf:
        for member in tf:
            # tar -C /data . ghi tên dạng ./wp-config.php (giữ dấu chấm của .htaccess)
            name = member.name.removeprefix("./").lstrip("/")
            if not name or name == "." or ".." in name.split("/"):
                continue
            arc = f"{prefix}/{name}"
            if member.isdir():
                zf.writestr(arc.rstrip("/") + "/", b"")
            elif member.isfile():
                info = zipfile.ZipInfo(arc, time.localtime(max(member.mtime, 315532800))[:6])
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (member.mode & 0o777) << 16
                with tf.extractfile(member) as f, zf.open(info, "w", force_zip64=True) as out:
                    shutil.copyfileobj(f, out, 1024 * 1024)


def portable(settings: Settings, path: Path) -> Path:
    """Chuyển bản sao lưu thành file .zip thông thường: <service>/ (thư mục web), databases/*.sql, README.txt.

    Trả file tạm, người gọi xóa sau khi gửi.
    """
    import gzip

    tmp = settings.data_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    archive.check_space(tmp, path.stat().st_size * 3)
    out = tmp / f"portable-{time.time_ns()}.zip"
    try:
        with tarfile.open(path) as tf, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            manifest = json.loads(tf.extractfile("manifest.json").read())
            for entry in manifest.get("services", []):
                svc = entry["name"]
                site = next((v for v in entry["volumes"] if v["site"]), None)
                if site:
                    with tf.extractfile(f"volumes/{site['volume']}.tar.gz") as f:
                        _zip_tree(zf, f, svc)
                elif entry.get("archive"):
                    with tf.extractfile(f"sources/{entry['archive']}") as f, \
                            zf.open(f"{svc}/{Path(entry.get('archive_name') or 'source.zip').name}", "w", force_zip64=True) as dst:
                        shutil.copyfileobj(f, dst, 1024 * 1024)
                elif entry.get("files"):
                    for member in tf.getmembers():
                        if member.isfile() and member.name.startswith(f"files/{svc}/"):
                            with tf.extractfile(member) as f, \
                                    zf.open(f"{svc}/{member.name.removeprefix(f'files/{svc}/')}", "w", force_zip64=True) as dst:
                                shutil.copyfileobj(f, dst, 1024 * 1024)
                for vol in entry["volumes"]:
                    if vol["site"]:
                        continue
                    # Thư mục dữ liệu nằm trong web (vd wp-content/uploads): đặt đúng chỗ trong thư mục web
                    rel = _relative(vol.get("mount"), site_files.MOUNT) if site else None
                    prefix = f"{svc}/{rel}" if rel else f"{svc}-data/{(vol.get('mount') or vol['volume']).strip('/')}"
                    with tf.extractfile(f"volumes/{vol['volume']}.tar.gz") as f:
                        _zip_tree(zf, f, prefix)
            for db_entry in manifest.get("databases", []):
                file = db_entry["file"]
                with tf.extractfile(f"databases/{file}") as f:
                    if file.endswith(".sql.gz"):
                        with gzip.open(f) as src, zf.open(f"databases/{db_entry['name']}.sql", "w", force_zip64=True) as dst:
                            shutil.copyfileobj(src, dst, 1024 * 1024)
                    else:
                        with zf.open(f"databases/{file}", "w", force_zip64=True) as dst:
                            shutil.copyfileobj(f, dst, 1024 * 1024)
            zf.writestr("README.txt", t("pbackup.readme", project=manifest.get("project", ""),
                                        created=manifest.get("created_at", "")[:19].replace("T", " ")))
    except Exception:
        out.unlink(missing_ok=True)
        raise
    return out
