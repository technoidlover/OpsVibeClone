import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from typing import Annotated
from urllib.parse import unquote

from fastapi import APIRouter, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import User, UserSession, utcnow
from ..security import DUMMY_PASSWORD_HASH, hash_password, hash_token, new_session_token, verify_password
from ..validators import Email
from ..i18n import t
from .deps import DB, SESSION_COOKIE, AppSettings, CurrentUser, client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])
logger = logging.getLogger(__name__)


class SetupRequest(BaseModel):
    setup_token: str = Field(max_length=200)
    email: Email
    password: str = Field(min_length=10, max_length=256)


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(max_length=256)


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


class UserOut(BaseModel):
    id: int
    email: str
    is_admin: bool


class _FailedLoginLimiter:
    """Chặn dò mật khẩu: tối đa N lần sai trong cửa sổ thời gian, theo IP và theo email."""

    def __init__(self, max_failures: int = 10, window_seconds: int = 900):
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: defaultdict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def blocked(self, *keys: str) -> bool:
        now = time.monotonic()
        with self._lock:
            return any(len(self._prune(k, now)) >= self.max_failures for k in keys)

    def record_failure(self, *keys: str) -> None:
        now = time.monotonic()
        with self._lock:
            for k in keys:
                self._prune(k, now).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._failures.pop(k, None)


login_limiter = _FailedLoginLimiter()


def ensure_setup_token(settings: Settings, db: Session) -> None:
    """Khi chưa có admin, sinh setup token và in ra log.

    Chỉ người đọc được log hoặc file trên server mới tạo được admin, nên panel
    vừa cài xong mà lộ ra Internet cũng không bị người lạ chiếm quyền.
    """
    path = settings.setup_token_path
    if db.scalar(select(func.count()).select_from(User)) > 0:
        path.unlink(missing_ok=True)
        return
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    token = path.read_text().strip()
    logger.warning("Chưa có tài khoản admin. Setup token: %s (lưu tại %s)", token, path)


def _start_session(db: DB, request: Request, response: Response, user: User, settings: AppSettings) -> None:
    token = new_session_token()
    ttl = timedelta(hours=settings.session_ttl_hours)
    db.execute(delete(UserSession).where(UserSession.expires_at <= utcnow()))
    db.add(UserSession(user_id=user.id, token_hash=hash_token(token), expires_at=utcnow() + ttl))
    db.commit()
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(ttl.total_seconds()),
        httponly=True,
        # Sau Traefik (HTTPS) thì cookie chỉ được gửi qua kết nối mã hóa
        secure=settings.secure_cookies or request.headers.get("x-forwarded-proto") == "https",
        samesite="strict",
        path="/",
    )


@router.get("/status")
def auth_status(db: DB) -> dict:
    """Frontend dùng để biết cần hiện màn hình tạo admin hay màn hình đăng nhập."""
    has_users = db.scalar(select(func.count()).select_from(User)) > 0
    return {"setup_required": not has_users}


@router.post("/setup", status_code=status.HTTP_201_CREATED)
def setup_admin(req: SetupRequest, request: Request, db: DB, response: Response, settings: AppSettings) -> UserOut:
    # Chỉ dùng được một lần, khi chưa có người dùng nào
    if db.scalar(select(func.count()).select_from(User)) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, t("auth.already_setup"))
    path = settings.setup_token_path
    expected = path.read_text().strip() if path.exists() else ""
    if not expected or not hmac.compare_digest(req.setup_token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("auth.setup_token_invalid"))
    user = User(email=req.email, password_hash=hash_password(req.password), is_admin=True)
    db.add(user)
    db.commit()
    path.unlink(missing_ok=True)
    _start_session(db, request, response, user, settings)
    return UserOut(id=user.id, email=user.email, is_admin=user.is_admin)


@router.post("/setup/restore", status_code=status.HTTP_202_ACCEPTED)
async def setup_restore(request: Request, db: DB, settings: AppSettings, x_setup_token: str = Header(..., max_length=200),
                        x_passphrase: str = Header(..., max_length=600), overwrite: bool = False) -> dict:
    """Panel mới cài: khôi phục từ file sao lưu toàn bộ thay vì tạo admin (cần setup token như tạo admin)."""
    from fastapi.concurrency import run_in_threadpool

    from .backup import receive_upload, stage_and_restart

    if db.scalar(select(func.count()).select_from(User)) > 0:
        raise HTTPException(status.HTTP_409_CONFLICT, t("auth.already_setup"))
    path = settings.setup_token_path
    expected = path.read_text().strip() if path.exists() else ""
    if not expected or not hmac.compare_digest(x_setup_token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_403_FORBIDDEN, t("auth.setup_token_invalid"))
    target = settings.data_dir / "restore-upload.vpbak"
    await receive_upload(request, target)
    try:
        return await run_in_threadpool(stage_and_restart, request, target, unquote(x_passphrase), overwrite)
    finally:
        target.unlink(missing_ok=True)


@router.post("/login")
def login(req: LoginRequest, request: Request, db: DB, response: Response, settings: AppSettings) -> UserOut:
    ip_key = f"ip:{client_ip(request)}"
    email_key = f"email:{req.email}"
    if login_limiter.blocked(ip_key, email_key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, t("auth.too_many_attempts"))

    user = db.scalar(select(User).where(User.email == req.email))
    valid = verify_password(req.password, user.password_hash if user else DUMMY_PASSWORD_HASH)
    if not user or not valid:
        login_limiter.record_failure(ip_key, email_key)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("auth.invalid_credentials"))

    login_limiter.reset(email_key)
    _start_session(db, request, response, user, settings)
    return UserOut(id=user.id, email=user.email, is_admin=user.is_admin)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request, db: DB, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == hash_token(token)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me")
def me(user: CurrentUser) -> UserOut:
    return UserOut(id=user.id, email=user.email, is_admin=user.is_admin)


@router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(req: ChangePasswordRequest, request: Request, user: CurrentUser, db: DB) -> None:
    key = f"password:{user.id}"
    if login_limiter.blocked(key):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, t("auth.too_many_attempts"))
    if not verify_password(req.current_password, user.password_hash):
        login_limiter.record_failure(key)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("auth.wrong_current_password"))
    user.password_hash = hash_password(req.new_password)
    # Đăng xuất mọi phiên khác, giữ phiên hiện tại
    current = hash_token(request.cookies.get(SESSION_COOKIE, ""))
    db.execute(delete(UserSession).where(UserSession.user_id == user.id, UserSession.token_hash != current))
    db.commit()
