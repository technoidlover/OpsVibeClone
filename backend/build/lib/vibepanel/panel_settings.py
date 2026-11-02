"""Cấu hình panel lưu trong DB, chỉnh được từ API mà không cần khởi động lại."""

import secrets

from sqlalchemy.orm import Session

from .models import Setting

PANEL_DOMAIN = "panel_domain"
ACME_EMAIL = "acme_email"
# Hậu tố tên miền mặc định cho app: <project>-<service>.<base_domain>
BASE_DOMAIN = "base_domain"
TRAEFIK_TOKEN = "traefik_token"
# Ngôn ngữ của log deploy và thông báo chạy nền (vi | en)
LANGUAGE = "language"


def get(db: Session, key: str) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else None


def put(db: Session, key: str, value: str | None) -> None:
    row = db.get(Setting, key)
    if value is None or value == "":
        if row:
            db.delete(row)
    elif row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))


def setdefault(db: Session, key: str, value: str | None) -> None:
    if value and get(db, key) is None:
        put(db, key, value)


def traefik_token(db: Session) -> str:
    token = get(db, TRAEFIK_TOKEN)
    if token is None:
        token = secrets.token_urlsafe(32)
        put(db, TRAEFIK_TOKEN, token)
        db.commit()
    return token


def sslip_domain(ip: str) -> str:
    """Tên miền wildcard miễn phí trỏ về IP, vd 1.2.3.4 -> 1-2-3-4.sslip.io."""
    return f"{ip.replace('.', '-')}.sslip.io"


def default_service_domain(base_domain: str | None, project: str, service: str) -> str | None:
    if not base_domain:
        return None
    label = f"{project}-{service}"
    # Mỗi nhãn DNS tối đa 63 ký tự
    return f"{label}.{base_domain}" if len(label) <= 63 else None
