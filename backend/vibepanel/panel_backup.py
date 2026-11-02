"""Sao lưu toàn bộ: một file chứa đủ để dựng lại panel và mọi project, khôi phục bằng một nút.

File .vpbak (mã hóa bằng passphrase, xem vault.py) là một tar gồm:
- opsvibes.db, secret.key: cấu hình panel (project, service, biến môi trường, tài khoản...);
- files/, archives/: file tự viết và mã nguồn tải lên bằng file nén (repo GitHub thì clone lại);
- databases/<id>.<đuôi>: bản dump mới nhất của từng database, lấy ngay lúc sao lưu;
- volumes/<tên volume>.tar.gz: dữ liệu của service (ảnh upload, SQLite...);
- manifest.json: danh sách các phần trên.

Khôi phục (từ trang Sao lưu, hoặc màn hình cài đặt của panel mới):
1. stage_restore: giải mã, kiểm tra, giải nén vào data_dir/.restore rồi panel tự khởi động lại;
2. apply_pending (lúc khởi động, trước khi mở DB): thay DB, secret.key, file; dữ liệu database /
   volume chuyển sang data_dir/restore-data;
3. RestoreRunner (chạy nền sau khi panel lên): tạo lại database và nạp dump, nạp volume, deploy lại
   service. Database / volume còn trên máy (panel lỗi nhưng VPS còn) được giữ nguyên, trừ khi
   người dùng chọn ghi đè.
"""

import json
import logging
import re
import shutil
import sqlite3
import tarfile
import threading
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import backups, offsite, panel_settings, vault
from .backups import BackupInfo, due
from .config import LEGACY_DB_FILE, Settings
from .crypto import SecretBox
from .runner import JobLogger

logger = logging.getLogger(__name__)

KEY = "panel_backup"
NAME_RE = re.compile(r"^panel-\d{8}-\d{6}\.vpbak$")
REMOTE = ("panel",)
FORMAT = 2
PANEL_FILES = ("opsvibes.db", "secret.key", "files", "archives")


def folder(settings: Settings) -> Path:
    return settings.data_dir / "backups" / "panel"


def restore_dir(settings: Settings) -> Path:
    return settings.data_dir / "restore-data"


def restore_log(settings: Settings) -> Path:
    return settings.data_dir / "logs" / "restore.log"


# ---------- Cài đặt ----------

def load(db: Session, box: SecretBox) -> dict:
    raw = panel_settings.get(db, KEY)
    cfg = json.loads(raw) if raw else {}
    passphrase = box.decrypt(cfg["passphrase_enc"]) if cfg.get("passphrase_enc") else None
    return {"enabled": cfg.get("enabled", False), "keep": cfg.get("keep", 7),
            "last_backup_at": cfg.get("last_backup_at"), "passphrase": passphrase}


def save(db: Session, box: SecretBox, cfg: dict) -> None:
    stored = {k: v for k, v in cfg.items() if k != "passphrase"}
    stored["passphrase_enc"] = box.encrypt(cfg["passphrase"]) if cfg.get("passphrase") else None
    panel_settings.put(db, KEY, json.dumps(stored))


def _info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                      offsite.status(path))


# ---------- Tạo bản sao lưu ----------

def create(db: Session, docker, settings: Settings, passphrase: str) -> BackupInfo:
    from . import service_backups
    from .models import Database, Service

    target = folder(settings)
    target.mkdir(parents=True, exist_ok=True)
    name = f"panel-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.vpbak"
    work = target / f".{name}.d"
    shutil.rmtree(work, ignore_errors=True)
    (work / "databases").mkdir(parents=True)
    (work / "volumes").mkdir()
    manifest: dict = {"format": FORMAT, "created_at": datetime.now(timezone.utc).isoformat(),
                      "databases": [], "skipped_databases": [], "volumes": []}
    plain = target / f".{name}.tar"
    try:
        # Bản sao nhất quán của SQLite trong lúc panel vẫn ghi (WAL)
        src, dst = sqlite3.connect(settings.db_path), sqlite3.connect(work / "opsvibes.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        if docker is not None:
            for database in db.scalars(select(Database)):
                if not backups.supported(database):
                    continue
                if docker.container_state(database.container) != "running":
                    manifest["skipped_databases"].append(f"{database.project.name}/{database.name}")
                    continue
                cmd, ext, compress = backups.export_cmd(database)
                file = f"{database.id}.{ext}"
                docker.exec_to_file(database.container, cmd, work / "databases" / file, compress,
                                    backups.dbs.IMPORT_TIMEOUT)
                manifest["databases"].append({"id": database.id, "name": f"{database.project.name}/{database.name}",
                                              "file": file})
            for service in db.scalars(select(Service)):
                for volume in service_backups.volumes(docker, service):
                    docker.volume_export(volume, work / "volumes" / f"{volume}.tar.gz", service_backups.TIMEOUT)
                    manifest["volumes"].append({"service_id": service.id, "volume": volume,
                                                "service": f"{service.project.name}/{service.name}"})
        (work / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
        with tarfile.open(plain, "w") as tf:
            for item in ("manifest.json", "opsvibes.db", "databases", "volumes"):
                tf.add(work / item, arcname=item)
            tf.add(settings.secret_key_path, arcname="secret.key")
            for sub in ("files", "archives"):
                if (settings.data_dir / sub).is_dir():
                    tf.add(settings.data_dir / sub, arcname=sub)
        vault.encrypt_file(plain, target / name, passphrase)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        plain.unlink(missing_ok=True)
    (target / name).chmod(0o600)
    return _info(target / name)


def run(db: Session, box: SecretBox, docker, settings: Settings, s3) -> BackupInfo:
    cfg = load(db, box)
    if not cfg["passphrase"]:
        raise ValueError("passphrase")
    created = create(db, docker, settings, cfg["passphrase"])
    offsite.upload(s3, folder(settings) / created.name, *REMOTE)
    if cfg["enabled"]:
        prune(settings, cfg["keep"])
        offsite.prune(s3, cfg["keep"], *REMOTE)
    cfg["last_backup_at"] = created.created_at.isoformat()
    save(db, box, cfg)
    db.commit()
    return _info(folder(settings) / created.name)


def run_if_due(db: Session, box: SecretBox, docker, settings: Settings, s3, now: datetime) -> BackupInfo | None:
    cfg = load(db, box)
    last = datetime.fromisoformat(cfg["last_backup_at"]) if cfg["last_backup_at"] else None
    if not cfg["enabled"] or not cfg["passphrase"] or not due(last, now):
        return None
    return run(db, box, docker, settings, s3)


def list_backups(settings: Settings) -> list[BackupInfo]:
    target = folder(settings)
    if not target.is_dir():
        return []
    return sorted((_info(p) for p in target.iterdir() if NAME_RE.fullmatch(p.name)), key=lambda b: b.name, reverse=True)


def path_of(settings: Settings, name: str) -> Path:
    path = folder(settings) / name
    if not NAME_RE.fullmatch(name) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete(settings: Settings, name: str) -> None:
    path = path_of(settings, name)
    path.unlink()
    offsite.forget(path)


def prune(settings: Settings, keep: int) -> None:
    for old in list_backups(settings)[keep:]:
        path = folder(settings) / old.name
        path.unlink(missing_ok=True)
        offsite.forget(path)


# ---------- Khôi phục: bước 1, chuẩn bị (panel đang chạy) ----------

def stage_restore(path: Path, passphrase: str, settings: Settings, overwrite: bool) -> dict:
    """Giải mã + kiểm tra + giải nén vào data_dir/.restore. Panel khởi động lại thì apply_pending áp dụng."""
    work = settings.data_dir / ".restore"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        plain = work / "bundle.tar"
        vault.decrypt_file(path, plain, passphrase)
        with tarfile.open(plain) as tf:
            members = tf.getmembers()
            for m in members:
                # Chỉ file / thư mục thường, đường dẫn tương đối không thoát ra ngoài
                if not (m.isfile() or m.isdir()) or m.name.startswith("/") or ".." in Path(m.name).parts:
                    raise vault.VaultError(f"Mục không hợp lệ trong bản sao lưu / invalid entry: {m.name}")
            tf.extractall(work / "data", members=members)
        plain.unlink()
        manifest_path = work / "data" / "manifest.json"
        # Bản sao lưu tạo trước khi đổi tên sản phẩm đặt DB là vibepanel.db
        legacy_db = work / "data" / LEGACY_DB_FILE
        if legacy_db.is_file() and not (work / "data" / "opsvibes.db").exists():
            legacy_db.rename(work / "data" / "opsvibes.db")
        if not manifest_path.is_file() or not (work / "data" / "opsvibes.db").is_file():
            raise vault.VaultError("Thiếu manifest hoặc DB của panel / missing manifest or panel DB")
        manifest = json.loads(manifest_path.read_text())
        (work / "PENDING").write_text(json.dumps({"overwrite": overwrite}))
        return manifest
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


# ---------- Bước 2: áp dụng lúc khởi động (trước khi mở DB) ----------

def apply_pending(settings: Settings) -> bool:
    work = settings.data_dir / ".restore"
    pending = work / "PENDING"
    if not pending.is_file():
        return False
    options = json.loads(pending.read_text())
    data = work / "data"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    old = settings.data_dir / f"before-restore-{stamp}"
    old.mkdir(parents=True)
    for name in PANEL_FILES:
        src, dst = data / name, settings.data_dir / name
        if dst.exists():
            dst.rename(old / name)
        if src.exists():
            src.rename(dst)
    # WAL / SHM của DB cũ không còn khớp với DB mới
    for suffix in ("-wal", "-shm"):
        (settings.data_dir / f"{settings.db_path.name}{suffix}").unlink(missing_ok=True)
    plan_dir = restore_dir(settings)
    shutil.rmtree(plan_dir, ignore_errors=True)
    plan_dir.mkdir(parents=True)
    for sub in ("databases", "volumes"):
        if (data / sub).is_dir():
            (data / sub).rename(plan_dir / sub)
    manifest = json.loads((data / "manifest.json").read_text())
    (plan_dir / "plan.json").write_text(json.dumps({**manifest, **options, "state": "pending"}, ensure_ascii=False))
    shutil.rmtree(work, ignore_errors=True)
    restore_log(settings).parent.mkdir(parents=True, exist_ok=True)
    restore_log(settings).unlink(missing_ok=True)
    logger.warning("Đã áp dụng bản sao lưu panel; cấu hình cũ giữ ở %s", old)
    return True


# ---------- Bước 3: dựng lại database / dữ liệu / service ----------

def restore_state(settings: Settings) -> dict:
    plan = restore_dir(settings) / "plan.json"
    state = json.loads(plan.read_text()).get("state") if plan.is_file() else None
    log = restore_log(settings)
    text = log.read_text(errors="replace")[-20000:] if log.is_file() else ""
    return {"state": state, "log": text}


class RestoreRunner:
    """Chạy nền sau khi panel khởi động với bản sao lưu vừa áp dụng."""

    def __init__(self, sessions, box: SecretBox, docker, settings: Settings, queue):
        self.sessions, self.box, self.docker, self.settings, self.queue = sessions, box, docker, settings, queue

    def start(self) -> bool:
        plan = restore_dir(self.settings) / "plan.json"
        if not plan.is_file() or json.loads(plan.read_text()).get("state") != "pending":
            return False
        if self.docker is None:
            self.run()  # chỉ khôi phục cấu hình, xong ngay
        else:
            threading.Thread(target=self.run, name="vp-restore", daemon=True).start()
        return True

    def _set_state(self, state: str) -> None:
        path = restore_dir(self.settings) / "plan.json"
        plan = json.loads(path.read_text())
        plan["state"] = state
        path.write_text(json.dumps(plan, ensure_ascii=False))

    def run(self) -> None:
        from .i18n import t

        self._set_state("running")
        log = JobLogger(restore_log(self.settings))
        try:
            self._run(log)
            self._set_state("done")
            log.write(t("restore.done"))
        except Exception as e:  # noqa: BLE001 - lỗi nào cũng phải hiện cho người dùng
            logger.exception("Khôi phục thất bại")
            self._set_state("failed")
            log.write(t("restore.failed", error=e))
        finally:
            log.close()

    def _run(self, log: JobLogger) -> None:
        from .api.databases import _run_import, _start_container
        from .i18n import t
        from .models import Database, JobKind, JobTrigger, Service

        plan_dir = restore_dir(self.settings)
        plan = json.loads((plan_dir / "plan.json").read_text())
        overwrite = plan.get("overwrite", False)
        docker = self.docker
        with self.sessions() as db:
            self._fix_base_domain(db, log)
            if docker is None:
                log.write(t("restore.no_docker"))
                return
            docker.ensure_network()
            # Database: tạo lại container (nếu thiếu), nạp dump khi máy chưa có dữ liệu hoặc chọn ghi đè
            for entry in plan.get("databases", []):
                database = db.get(Database, entry["id"])
                if database is None:
                    continue
                label = f"{database.project.name}/{database.name}"
                had_data = docker.volume_exists(database.volume)
                if docker.container_state(database.container) != "running":
                    log.write(t("restore.db_start", name=label))
                    docker.remove_container(database.container)
                    _start_container(self.sessions, self.box, docker, self.settings, database.id)
                if had_data and not overwrite:
                    log.write(t("restore.db_kept", name=label))
                    continue
                log.write(t("restore.db_import", name=label))
                database.import_status, database.error = "running", None
                db.commit()
                _run_import(self.sessions, self.box, docker, self.settings, database.id,
                            plan_dir / "databases" / entry["file"], entry["file"], clean=True)
                db.refresh(database)
                log.write(t("restore.db_result", name=label, status=database.import_status,
                            error=database.error or ""))
            # Dữ liệu service (volume)
            restored_services: set[int] = set()
            for entry in plan.get("volumes", []):
                volume, path = entry["volume"], plan_dir / "volumes" / f"{entry['volume']}.tar.gz"
                if docker.volume_exists(volume) and not overwrite:
                    log.write(t("restore.volume_kept", volume=volume))
                    continue
                service = db.get(Service, entry["service_id"])
                if service is not None and overwrite:
                    for container in docker.containers_for_service(service.id):
                        docker.stop(container)
                log.write(t("restore.volume_import", volume=volume))
                with path.open("rb") as f:
                    docker.volume_import(volume, f, 6 * 3600)
                restored_services.add(entry["service_id"])
            # Service: deploy lại cái nào chưa chạy (máy mới) hoặc vừa được nạp lại dữ liệu
            from .models import Job

            for service in db.scalars(select(Service)).all():
                active = db.get(Job, service.active_job_id) if service.active_job_id else None
                running = bool(active and active.container and docker.container_state(active.container) == "running")
                if running and service.id not in restored_services:
                    log.write(t("restore.service_running", name=f"{service.project.name}/{service.name}"))
                    continue
                if service.source == "archive" and not self.settings.archive_path(service.id).is_file():
                    log.write(t("restore.service_no_source", name=f"{service.project.name}/{service.name}"))
                    continue
                if service.source == "manual" and not self.settings.files_dir(service.id).is_dir():
                    continue
                log.write(t("restore.service_deploy", name=f"{service.project.name}/{service.name}"))
                self.queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.MANUAL)
        shutil.rmtree(plan_dir / "databases", ignore_errors=True)
        shutil.rmtree(plan_dir / "volumes", ignore_errors=True)

    def _fix_base_domain(self, db: Session, log: JobLogger) -> None:
        """Bản sao lưu từ VPS khác: tên miền mặc định sslip.io phải theo IP của máy mới."""
        from .i18n import t

        ip = self.settings.public_ip
        current = panel_settings.get(db, panel_settings.BASE_DOMAIN)
        if ip and current and current.endswith(".sslip.io") and current != panel_settings.sslip_domain(ip):
            panel_settings.put(db, panel_settings.BASE_DOMAIN, panel_settings.sslip_domain(ip))
            db.commit()
            log.write(t("restore.base_domain", domain=panel_settings.sslip_domain(ip)))


# ---------- Khôi phục từ dòng lệnh ----------

def restore_cli(argv: list[str] | None = None) -> int:
    """docker exec -it opsvibes opsvibes-restore FILE [--overwrite], rồi docker restart opsvibes.

    FILE là đường dẫn trong container (/etc/opsvibes/... trên server) hoặc tên một bản trong backups/panel.
    """
    import argparse
    import getpass
    import sys

    from .config import get_settings

    parser = argparse.ArgumentParser(prog="opsvibes-restore", description="Khôi phục panel từ file .vpbak / Restore the panel from a .vpbak file")
    parser.add_argument("file", help="đường dẫn file .vpbak, hoặc tên bản sao lưu trong backups/panel")
    parser.add_argument("--overwrite", action="store_true", help="ghi đè database / volume còn trên máy")
    args = parser.parse_args(argv)

    settings = get_settings()
    path = Path(args.file)
    if not path.is_file() and (folder(settings) / args.file).is_file():
        path = folder(settings) / args.file
    if not path.is_file():
        print(f"Không thấy file {args.file} / File not found. Chép file vào /etc/opsvibes trên server rồi chạy lại.", file=sys.stderr)
        return 1

    passphrase = getpass.getpass("Passphrase: ")
    try:
        manifest = stage_restore(path, passphrase, settings, args.overwrite)
    except vault.VaultError as e:
        print(e, file=sys.stderr)
        return 1
    except (tarfile.TarError, ValueError, OSError) as e:
        print(f"File sao lưu không hợp lệ / Invalid backup file: {e}", file=sys.stderr)
        return 1

    print(f"Bản sao lưu lúc {manifest.get('created_at')}: {len(manifest.get('databases', []))} database, "
          f"{len(manifest.get('volumes', []))} volume")
    print("Đã chuẩn bị khôi phục. Chạy `docker restart opsvibes` để áp dụng /")
    print("Restore staged. Run `docker restart opsvibes` to apply it")
    return 0
