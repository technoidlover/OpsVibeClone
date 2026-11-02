"""Mã nguồn website PHP tải lên bằng file nén chạy từ một Docker volume (giống public_html của hosting).

Image Nixpacks chép mã nguồn vào /app lúc build, nên mọi thay đổi trên web (cài / cập nhật plugin,
theme, WordPress core, file do code PHP tự ghi) chỉ nằm trong container và mất khi deploy lại.
Với service nguồn archive build bằng Nixpacks mà là PHP, panel gắn một volume vào /app:
- lần đầu (hoặc vừa tải lên file nén mới): volume mới, rỗng, Docker tự chép /app của image vừa build
  vào đó; deploy thành công thì volume cũ bị xóa, deploy lỗi thì volume mới bị xóa và bản cũ chạy lại;
- các lần deploy khác (đổi biến môi trường, khởi động lại...): dùng lại volume đang có, giữ nguyên
  mọi thay đổi.
Volume này là "mã nguồn" trong sao lưu project / dữ liệu service, nên bản sao lưu có đủ website đang chạy.
"""

from pathlib import Path

from .config import Settings
from .models import Service

MOUNT = "/app"
# php-fpm (Nixpacks) chạy bằng user nobody: cho ghi được toàn bộ mã nguồn như trên hosting
CHOWN_PREFIX = "chown -R nobody:nobody /app 2>/dev/null; "


def prefix(service: Service) -> str:
    return f"vp_{service.slug}_sitefiles-"


def new_name(service: Service, tag: str | int) -> str:
    return f"{prefix(service)}{tag}"


def is_site_volume(service: Service, volume: str) -> bool:
    return volume.startswith(prefix(service))


def wanted(service: Service, build_dir: Path) -> bool:
    """Service dùng volume mã nguồn: tải lên bằng file nén, build bằng Nixpacks, là PHP."""
    if service.source != "archive" or service.image or service.build_mode == "compose":
        return False
    if (build_dir / "Dockerfile").is_file():
        return False
    return any((build_dir / name).is_file() for name in ("index.php", "composer.json", "wp-settings.php"))


def fresh_marker(settings: Settings, service: Service) -> Path:
    """Có file này: vừa tải lên file nén mới, lần deploy tới phải dựng lại volume từ mã nguồn mới."""
    return settings.archive_path(service.id).with_suffix(".fresh")
