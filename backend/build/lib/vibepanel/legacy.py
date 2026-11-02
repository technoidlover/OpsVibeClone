"""Chuyển bản cài cũ (tên VibePanel) sang tên OpsVibes, chạy mỗi lần panel khởi động.

Mọi bước đều idempotent: bản cài mới hoặc đã chuyển xong thì không làm gì.
Thư mục /etc/vibepanel -> /etc/opsvibes do script deploy / cài đặt chuyển trên host
(panel chỉ thấy thư mục đã mount), ở đây chỉ còn các thứ bên trong và phía Docker.
"""

import logging

from .config import LEGACY_DB_FILE, LEGACY_NAME, LEGACY_TRAEFIK_CONTAINER, Settings
from .docker import Docker, DockerError

logger = logging.getLogger(__name__)

LEGACY_ACME_VOLUME = "vibepanel-acme"


def migrate_files(settings: Settings) -> None:
    """vibepanel.db (kèm -wal / -shm của SQLite) -> opsvibes.db. Chạy trước khi mở DB."""
    old = settings.data_dir / LEGACY_DB_FILE
    if not old.exists() or settings.db_path.exists():
        return
    for suffix in ("-wal", "-shm", ""):
        src = settings.data_dir / f"{LEGACY_DB_FILE}{suffix}"
        if src.exists():
            src.rename(settings.data_dir / f"{settings.db_path.name}{suffix}")
    logger.warning("Đã đổi %s thành %s", LEGACY_DB_FILE, settings.db_path.name)


def migrate_network(docker: Docker, settings: Settings) -> None:
    """App và database tạo trên network cũ: tạo lại trên network mới với cùng alias, volume và cấu hình.

    Chỉ nối thêm network mới là chưa đủ: container vẫn ghi NetworkMode = network cũ nên sau khi
    xóa network cũ, Docker không khởi động lại được nó (restart, reboot server). App bind vào IP
    cũ (vd Next.js theo HOSTNAME) cũng cần khởi động lại để nhận IP mới. Chạy trước khi nhận job.
    """
    try:
        # Traefik cũ giữ cổng 80/443: gỡ để panel dựng lại Traefik mới trên network mới
        docker.remove_container(LEGACY_TRAEFIK_CONTAINER)
        names = docker.containers_on_network_mode(LEGACY_NAME)
    except DockerError as e:
        logger.error("Không đọc được container trên network %s: %s", LEGACY_NAME, e)
        return
    for name in names:
        try:
            docker.recreate_on_network(name)
            logger.warning("Đã tạo lại %s trên network %s", name, settings.network)
        except DockerError as e:
            logger.error("Không tạo lại được %s trên network %s: %s", name, settings.network, e)
    try:
        # Container chỉ nối phụ vào network cũ (không tạo bằng --network): nối sang network mới
        moved = docker.migrate_network(LEGACY_NAME)
    except DockerError as e:
        logger.error("Không chuyển được network %s: %s", LEGACY_NAME, e)
        return
    if moved:
        logger.warning("Đã chuyển %d container sang network %s: %s", len(moved), settings.network, ", ".join(moved))


def migrate_acme(docker: Docker, acme_volume: str) -> None:
    """Chép chứng chỉ Let's Encrypt sang volume mới để không phải xin lại. Chạy trước khi dựng Traefik."""
    if not docker.volume_exists(LEGACY_ACME_VOLUME) or docker.volume_exists(acme_volume):
        return
    try:
        docker.volume_copy(LEGACY_ACME_VOLUME, acme_volume)
    except DockerError as e:
        # Không chép được thì Traefik tự xin chứng chỉ mới
        logger.error("Không chép được chứng chỉ từ %s: %s", LEGACY_ACME_VOLUME, e)
        return
    docker.remove_volume(LEGACY_ACME_VOLUME)
    logger.warning("Đã chuyển chứng chỉ từ %s sang %s", LEGACY_ACME_VOLUME, acme_volume)
