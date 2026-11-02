import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from typing import Literal

from .. import github, i18n, panel_settings, proxy
from ..db import session_factory
from ..i18n import t
from ..validators import Domain, Email
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep

router = APIRouter(tags=["system"])
logger = logging.getLogger(__name__)


class PanelSettings(BaseModel):
    """Bỏ trống (null) một trường để xóa giá trị đó."""

    panel_domain: Domain | None = None
    base_domain: Domain | None = None
    acme_email: Email | None = None


def _read(db: DB) -> PanelSettings:
    return PanelSettings(
        panel_domain=panel_settings.get(db, panel_settings.PANEL_DOMAIN),
        base_domain=panel_settings.get(db, panel_settings.BASE_DOMAIN),
        acme_email=panel_settings.get(db, panel_settings.ACME_EMAIL),
    )


def refresh_traefik(docker, settings) -> None:
    try:
        with session_factory()() as db:
            proxy.ensure_traefik(docker, settings, db)
    except Exception:  # noqa: BLE001 - lỗi Traefik không được làm hỏng request
        logger.exception("Không cập nhật được Traefik")


def update_github_webhook(box, domain: str) -> None:
    """Tên miền panel đổi: trỏ webhook của GitHub App về tên miền mới."""
    try:
        with session_factory()() as db:
            app = github.get_app(db)
            if app is not None:
                github.set_webhook_url(app, box, f"https://{domain}")
                logger.info("Đã đổi webhook GitHub App sang https://%s", domain)
    except Exception:  # noqa: BLE001 - lỗi GitHub không được làm hỏng việc lưu cài đặt
        logger.exception("Không đổi được webhook của GitHub App")


@router.get("/api/settings")
def get_panel_settings(db: DB, _user: CurrentUser) -> PanelSettings:
    return _read(db)


@router.put("/api/settings")
def update_panel_settings(
    req: PanelSettings,
    db: DB,
    _user: CurrentUser,
    settings: AppSettings,
    docker: DockerDep,
    background: BackgroundTasks,
    box: Box,
) -> PanelSettings:
    email_changed = req.acme_email != panel_settings.get(db, panel_settings.ACME_EMAIL)
    domain_changed = req.panel_domain != panel_settings.get(db, panel_settings.PANEL_DOMAIN)
    panel_settings.put(db, panel_settings.PANEL_DOMAIN, req.panel_domain)
    panel_settings.put(db, panel_settings.BASE_DOMAIN, req.base_domain)
    panel_settings.put(db, panel_settings.ACME_EMAIL, req.acme_email)
    db.commit()
    # Tên miền: Traefik tự đọc lại sau 5 giây. Email ACME nằm trong cấu hình
    # tĩnh nên phải tạo lại container Traefik.
    if email_changed and docker is not None:
        background.add_task(refresh_traefik, docker, settings)
    if domain_changed and req.panel_domain:
        background.add_task(update_github_webhook, box, req.panel_domain)
    return _read(db)


class UpdateRequest(BaseModel):
    version: str = Field(max_length=32)


@router.get("/api/system/update")
def update_info(request: Request, _user: CurrentUser) -> dict:
    """Phiên bản đang chạy, bản mới nhất trên registry và tiến độ cập nhật (nếu có)."""
    return request.app.state.updates.info()


@router.post("/api/system/update/check")
def check_update(request: Request, _user: CurrentUser) -> dict:
    request.app.state.updates.check()
    return request.app.state.updates.info()


@router.post("/api/system/update", status_code=status.HTTP_202_ACCEPTED)
def start_update(req: UpdateRequest, request: Request, _user: CurrentUser) -> dict:
    try:
        request.app.state.updates.begin(req.version)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("system.update_not_newer", version=req.version)) from None
    except RuntimeError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, t(f"system.update_{e}")) from None
    return request.app.state.updates.info()


class LanguageUpdate(BaseModel):
    language: Literal["vi", "en"]


@router.put("/api/settings/language", status_code=status.HTTP_204_NO_CONTENT)
def update_language(req: LanguageUpdate, db: DB, _user: CurrentUser) -> None:
    """Ngôn ngữ mặc định của panel: dùng cho log deploy, webhook và việc chạy nền."""
    if panel_settings.get(db, panel_settings.LANGUAGE) != req.language:
        panel_settings.put(db, panel_settings.LANGUAGE, req.language)
        db.commit()
    i18n.set_panel_lang(req.language)


@router.get("/api/internal/traefik", include_in_schema=False)
def traefik_config(db: DB, settings: AppSettings, token: str = Query("")) -> dict:
    """Cấu hình route động cho Traefik (HTTP provider)."""
    if not hmac.compare_digest(token.encode(), panel_settings.traefik_token(db).encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("system.traefik_token_invalid"))
    return proxy.dynamic_config(db, settings)
