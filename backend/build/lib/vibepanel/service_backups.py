"""Sao lưu dữ liệu (Docker volume) của service: ảnh upload, file SQLite, dữ liệu của app 1-click...

Mỗi bản là một file tar ở data_dir/backups/services/<service_id>/<thời điểm>.tar, bên trong mỗi
volume một file <tên volume>.tar.gz. Volume được đọc chỉ-đọc trong lúc app vẫn chạy (database
dạng file như SQLite nên dùng tính năng sao lưu database riêng nếu cần nhất quán tuyệt đối).
Khôi phục chạy như một job (JobKind.RESTORE): dừng container, ghi đè volume, chạy lại.
"""

import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import compose, offsite, site_files, templates
from .backups import BackupInfo
from .config import Settings
from .i18n import t
from .models import Service

NAME_RE = re.compile(r"^\d{8}-\d{6}\.tar$")
TIMEOUT = 6 * 3600


def folder(settings: Settings, service: Service) -> Path:
    return settings.data_dir / "backups" / "services" / str(service.id)


def remote_parts(service: Service) -> tuple[str, ...]:
    return ("services", service.project.name, service.name)


def mounts(docker, service: Service) -> dict[str, str | None]:
    """Volume của service -> thư mục gắn trong container (None: volume của compose project).

    Gồm volume mã nguồn website (site_files.py) và các thư mục dữ liệu khai báo trong panel.
    """
    if service.build_mode == "compose" and not service.image:
        return dict.fromkeys(docker.volumes_with_label(f"com.docker.compose.project={compose.project_name(service)}"))
    out: dict[str, str | None] = {}
    if service.site_volume and docker.volume_exists(service.site_volume):
        out[service.site_volume] = site_files.MOUNT
    for mount in service.volumes:
        name = templates.volume_name(service.slug, mount)
        if docker.volume_exists(name):
            out[name] = mount
    return out


def volumes(docker, service: Service) -> list[str]:
    return list(mounts(docker, service))


def target(service: Service, volume: str) -> str | None:
    """Volume hiện tại để nạp dữ liệu của `volume` trong bản sao lưu; None: không thuộc service này.

    Volume mã nguồn đổi tên mỗi lần tải lên file nén mới: nạp vào volume mã nguồn đang dùng.
    """
    if site_files.is_site_volume(service, volume):
        return service.site_volume
    if volume in {templates.volume_name(service.slug, m) for m in service.volumes}:
        return volume
    if service.build_mode == "compose" and not service.image and volume.startswith(f"{compose.project_name(service)}_"):
        return volume
    return None


def _info(path: Path) -> BackupInfo:
    stat = path.stat()
    return BackupInfo(path.name, stat.st_size, datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                      offsite.status(path))


def create(docker, settings: Settings, service: Service) -> BackupInfo:
    names = volumes(docker, service)
    if not names:
        raise ValueError(t("backup.no_volumes"))
    target_dir = folder(settings, service)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.tar"
    temp = target_dir / f".{name}.part"
    parts: list[Path] = []
    try:
        with tarfile.open(temp, "w") as outer:
            for volume in names:
                part = target_dir / f".{name}.{volume}.tar.gz"
                parts.append(part)
                docker.volume_export(volume, part, TIMEOUT)
                outer.add(part, arcname=f"{volume}.tar.gz")
                part.unlink()
        temp.rename(target_dir / name)
    finally:
        temp.unlink(missing_ok=True)
        for part in parts:
            part.unlink(missing_ok=True)
    return _info(target_dir / name)


def run(docker, settings: Settings, s3, service: Service) -> BackupInfo:
    """Tạo bản sao lưu, tải lên S3 (nếu có), giữ N bản nếu bật tự động. Người gọi commit DB."""
    created = create(docker, settings, service)
    path = folder(settings, service) / created.name
    offsite.upload(s3, path, *remote_parts(service))
    if service.backup_enabled:
        prune(settings, service, service.backup_keep)
        offsite.prune(s3, service.backup_keep, *remote_parts(service))
    service.last_backup_at = created.created_at
    return _info(path)


def list_backups(settings: Settings, service: Service) -> list[BackupInfo]:
    target_dir = folder(settings, service)
    if not target_dir.is_dir():
        return []
    return sorted((_info(p) for p in target_dir.iterdir() if NAME_RE.fullmatch(p.name)),
                  key=lambda b: b.name, reverse=True)


def path_of(settings: Settings, service: Service, name: str) -> Path:
    path = folder(settings, service) / name
    if not NAME_RE.fullmatch(name) or not path.is_file():
        raise FileNotFoundError(name)
    return path


def delete(settings: Settings, service: Service, name: str) -> None:
    path = path_of(settings, service, name)
    path.unlink()
    offsite.forget(path)


def prune(settings: Settings, service: Service, keep: int) -> None:
    for old in list_backups(settings, service)[keep:]:
        path = folder(settings, service) / old.name
        path.unlink(missing_ok=True)
        offsite.forget(path)


def remove_all(settings: Settings, service: Service) -> None:
    shutil.rmtree(folder(settings, service), ignore_errors=True)


def total_size(settings: Settings, service: Service) -> int:
    return sum(b.size for b in list_backups(settings, service))


def restore(ctx) -> None:
    """Pipeline của job khôi phục: tên bản sao lưu nằm trong job.prompt."""
    service, docker, settings = ctx.service, ctx.docker, ctx.settings
    name = ctx.job.prompt or ""
    path = folder(settings, service) / name
    if not NAME_RE.fullmatch(name):
        raise RuntimeError(t("backup.not_found"))
    if not path.is_file():
        # Chỉ còn ở nơi lưu ngoài (vd panel dựng lại trên máy mới): tải về trước
        s3 = offsite.client(ctx.db, ctx.box)
        if s3 is None:
            raise RuntimeError(t("backup.not_found"))
        ctx.log.write(t("backup.downloading", name=name))
        path.parent.mkdir(parents=True, exist_ok=True)
        s3.get_file(s3.key(*remote_parts(service), name), path)
    ctx.log.write(t("backup.restore_start", name=name))
    containers = docker.containers_for_service(service.id)
    for container in containers:
        ctx.log.write(t("backup.stop", container=container))
        docker.stop(container)
    try:
        with tarfile.open(path) as outer:
            for member in outer:
                if not member.isfile() or not member.name.endswith(".tar.gz"):
                    continue
                volume = target(service, member.name.removesuffix(".tar.gz"))
                if volume is None:
                    continue
                ctx.log.write(t("backup.restore_volume", volume=volume, size=f"{member.size / 1024 / 1024:.1f}"))
                with outer.extractfile(member) as src:
                    docker.volume_import(volume, src, TIMEOUT)
    finally:
        for container in containers:
            ctx.log.write(t("backup.start", container=container))
            try:
                docker.start(container)
            except Exception as e:  # noqa: BLE001 - khởi động lại lỗi vẫn báo trong log, không che lỗi khôi phục
                ctx.log.write(t("deploy.restart_old_failed", name=container, error=e))
