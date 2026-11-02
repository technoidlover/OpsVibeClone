import time
from typing import Annotated

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..crypto import SecretBox
from ..db import get_db
from ..docker import Docker
from ..i18n import t
from ..jobs import JobQueue
from ..models import Project, Service, User, UserSession, utcnow
from ..security import hash_token

SESSION_COOKIE = "opsvibes_session"

DB = Annotated[Session, Depends(get_db)]


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_box(request: Request) -> SecretBox:
    return request.app.state.box


def get_queue(request: Request) -> JobQueue:
    return request.app.state.queue


def get_docker(request: Request) -> Docker | None:
    return request.app.state.docker


# (IP của Traefik, thời điểm tra cứu); None = chưa tra lần nào
_proxy_ip_cache: tuple[str | None, float | None] = (None, None)


def client_ip(request: Request) -> str:
    """IP thật của người dùng.

    Request đi qua Traefik thì peer luôn là IP của Traefik; chỉ khi đó mới tin
    header X-Real-Ip. Request gọi thẳng cổng 3000 không thể giả mạo IP.
    """
    global _proxy_ip_cache
    peer = request.client.host if request.client else "unknown"
    docker: Docker | None = request.app.state.docker
    if docker is None:
        return peer
    proxy_ip, checked_at = _proxy_ip_cache
    if checked_at is None or time.monotonic() - checked_at > 60:
        proxy_ip = docker.resolve_ip(request.app.state.settings.traefik_container)
        _proxy_ip_cache = (proxy_ip, time.monotonic())
    if proxy_ip and peer == proxy_ip:
        return request.headers.get("x-real-ip", peer)
    return peer


AppSettings = Annotated[Settings, Depends(get_settings)]
Box = Annotated[SecretBox, Depends(get_box)]
Queue = Annotated[JobQueue, Depends(get_queue)]
DockerDep = Annotated[Docker | None, Depends(get_docker)]


def get_current_user(
    db: DB,
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> User:
    if session_token:
        sess = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(session_token)))
        if sess and sess.expires_at > utcnow():
            return sess.user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.not_logged_in"))


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_project(db: DB, _user: CurrentUser, project: str) -> Project:
    obj = db.scalar(select(Project).where(Project.name == project))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("project.not_found"))
    return obj


ProjectDep = Annotated[Project, Depends(get_project)]


def get_service(db: DB, project: ProjectDep, service: str) -> Service:
    obj = db.scalar(select(Service).where(Service.project_id == project.id, Service.name == service))
    if obj is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("service.not_found"))
    return obj


ServiceDep = Annotated[Service, Depends(get_service)]
