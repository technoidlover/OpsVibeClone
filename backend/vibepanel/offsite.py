"""Nơi lưu sao lưu ngoài VPS (S3-compatible hoặc Google Drive): cấu hình, tải lên, liệt kê, giữ N bản.

Cấu hình lưu trong bảng settings, phần bí mật (secret key S3, client secret + refresh token
Google) mã hóa bằng SecretBox. Hai loại client cùng giao diện (key, put_file, get_file, list,
delete, check) nên phần còn lại không cần biết đang dùng loại nào. Trạng thái tải lên của
từng file sao lưu cục bộ ghi vào file kèm theo: "<tên>.s3" (đã lên S3) hoặc "<tên>.s3err" (lỗi).
Key trên S3: <prefix>/<loại>/<...>/<tên file>, đặt theo tên project / database / service để
panel dựng lại trên máy mới (cùng tên) vẫn thấy và khôi phục được.
"""

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from . import panel_settings
from .crypto import SecretBox
from .gdrive import Drive, DriveError
from .s3 import S3, S3Error, S3Object

logger = logging.getLogger(__name__)

KEY = "backup_offsite"
# Trường bí mật theo loại: lưu mã hóa, không bao giờ trả ra API
SECRETS = {"s3": ("secret_key",), "gdrive": ("client_secret", "refresh_token")}
ERRORS = (S3Error, DriveError, OSError)


def load(db: Session, box: SecretBox) -> dict | None:
    raw = panel_settings.get(db, KEY)
    if not raw:
        return None
    cfg = json.loads(raw)
    cfg.update(json.loads(box.decrypt(cfg.pop("secrets_enc"))))
    return cfg


def save(db: Session, box: SecretBox, cfg: dict | None) -> None:
    if cfg is None:
        panel_settings.put(db, KEY, None)
        return
    names = SECRETS[cfg["type"]]
    stored = {k: v for k, v in cfg.items() if k not in names}
    stored["secrets_enc"] = box.encrypt(json.dumps({k: cfg[k] for k in names}))
    panel_settings.put(db, KEY, json.dumps(stored))


def public(cfg: dict | None) -> dict | None:
    """Cấu hình trả cho giao diện: bỏ mọi trường bí mật."""
    if cfg is None:
        return None
    return {k: v for k, v in cfg.items() if k not in SECRETS[cfg["type"]]}


def client_from(cfg: dict):
    if cfg["type"] == "gdrive":
        return Drive(cfg["client_id"], cfg["client_secret"], cfg["refresh_token"], cfg.get("folder") or "OpsVibes")
    return S3(cfg["endpoint"], cfg.get("region") or "auto", cfg["bucket"], cfg["access_key"], cfg["secret_key"],
              cfg.get("prefix") or "")


def client(db: Session, box: SecretBox):
    cfg = load(db, box)
    return client_from(cfg) if cfg else None


# ---------- Trạng thái tải lên của file cục bộ ----------

def status(path: Path) -> str | None:
    """"ok" | "failed" | None (chưa tải / chưa cấu hình S3)."""
    if path.with_name(path.name + ".s3").exists():
        return "ok"
    if path.with_name(path.name + ".s3err").exists():
        return "failed"
    return None


def error(path: Path) -> str | None:
    marker = path.with_name(path.name + ".s3err")
    return marker.read_text()[:500] if marker.exists() else None


def forget(path: Path) -> None:
    for suffix in (".s3", ".s3err"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)


def upload(s3, path: Path, *parts: str) -> str | None:
    """Tải file sao lưu lên nơi lưu ngoài (nếu đã cấu hình), ghi trạng thái. Không ném lỗi: bản cục bộ vẫn giữ."""
    if s3 is None:
        return None
    forget(path)
    try:
        s3.put_file(s3.key(*parts, path.name), path)
    except ERRORS as e:
        logger.warning("Tải %s lên nơi lưu ngoài thất bại: %s", path.name, e)
        path.with_name(path.name + ".s3err").write_text(str(e))
        return "failed"
    path.with_name(path.name + ".s3").write_text("")
    return "ok"


def remote_list(s3, *parts: str) -> list[S3Object]:
    return s3.list(s3.key(*parts))


def prune(s3, keep: int, *parts: str) -> None:
    """Giữ `keep` bản mới nhất ở nơi lưu ngoài (tên file theo thời điểm nên sắp theo tên là theo thời gian)."""
    if s3 is None:
        return
    try:
        items = sorted(remote_list(s3, *parts), key=lambda o: o.key, reverse=True)
        for old in items[keep:]:
            s3.delete(old.key)
    except ERRORS as e:
        logger.warning("Dọn bản sao lưu cũ ở nơi lưu ngoài thất bại: %s", e)
