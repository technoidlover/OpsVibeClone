"""Kết nối GitHub (GitHub App) và webhook chung của App."""

import hashlib
import hmac
import json
import urllib.parse

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from .. import github
from ..i18n import t
from ..models import JobKind, JobTrigger, Service
from .deps import DB, Box, CurrentUser, Queue
from .webhooks import MAX_WEBHOOK_BODY

router = APIRouter(prefix="/api/github", tags=["github"])


class InstallationOut(BaseModel):
    id: int
    account: str
    account_type: str
    avatar_url: str | None
    repository_selection: str
    settings_url: str


class GitHubStatus(BaseModel):
    connected: bool
    app_name: str | None = None
    app_url: str | None = None
    owner: str | None = None
    install_url: str | None = None
    installations: list[InstallationOut] = []
    error: str | None = None


class ManifestRequest(BaseModel):
    # Địa chỉ panel mà trình duyệt đang mở; GitHub chuyển hướng và gửi webhook về đây
    origin: str = Field(max_length=300)
    organization: str | None = Field(None, max_length=100, pattern=r"^[A-Za-z0-9-]*$")

    @field_validator("origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        parts = urllib.parse.urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/"):
            raise ValueError(t("github.origin_invalid"))
        return f"{parts.scheme}://{parts.netloc}"


class ManifestOut(BaseModel):
    action_url: str
    manifest: str


class RepoOut(BaseModel):
    id: int
    installation_id: int
    full_name: str
    name: str
    owner: str
    private: bool
    default_branch: str
    clone_url: str
    html_url: str
    description: str | None
    pushed_at: str | None


def _app_or_404(db: DB):
    app = github.get_app(db)
    if app is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("github.not_connected"))
    return app


def _bad_gateway(e: github.GitHubError) -> HTTPException:
    return HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


@router.get("/status")
def github_status(db: DB, box: Box, _user: CurrentUser) -> GitHubStatus:
    app = github.get_app(db)
    if app is None:
        return GitHubStatus(connected=False)
    out = GitHubStatus(
        connected=True, app_name=app.name, app_url=app.html_url, owner=app.owner_login,
        install_url=github.install_url(app),
    )
    try:
        out.installations = [InstallationOut(**vars(i)) for i in github.installations(app, box)]
    except github.GitHubError as e:
        out.error = str(e)
    return out


@router.post("/manifest")
def create_manifest(req: ManifestRequest, db: DB, _user: CurrentUser) -> ManifestOut:
    """Trình duyệt POST manifest này sang GitHub để tạo App (GitHub yêu cầu form POST)."""
    state = github.new_state(db)
    return ManifestOut(
        action_url=github.manifest_action(req.organization or None, state),
        manifest=json.dumps(github.manifest(req.origin)),
    )


@router.get("/manifest/callback")
def manifest_callback(db: DB, box: Box, code: str = Query(max_length=200), state: str = Query("", max_length=200)):
    # Không có cookie phiên ở bước này (SameSite=strict): state thay cho xác thực
    if not github.consume_state(db, state):
        return RedirectResponse("/settings?github=expired", status.HTTP_303_SEE_OTHER)
    try:
        app = github.convert_manifest(db, box, code)
    except github.GitHubError:
        return RedirectResponse("/settings?github=error", status.HTTP_303_SEE_OTHER)
    # Tạo App xong thì cài luôn lên tài khoản và chọn repo
    return RedirectResponse(github.install_url(app), status.HTTP_303_SEE_OTHER)


@router.get("/setup")
def after_install() -> RedirectResponse:
    """GitHub quay về đây sau khi cài/đổi quyền App; giao diện tự tải lại danh sách."""
    return RedirectResponse("/settings?github=installed", status.HTTP_303_SEE_OTHER)


@router.delete("/app", status_code=status.HTTP_204_NO_CONTENT)
def disconnect(db: DB, _user: CurrentUser) -> None:
    """Xóa khóa App khỏi panel. App trên GitHub cần xóa thủ công trong cài đặt GitHub."""
    app = _app_or_404(db)
    db.delete(app)
    db.commit()
    github._tokens.clear()


@router.get("/repos")
def list_repos(db: DB, box: Box, _user: CurrentUser) -> list[RepoOut]:
    try:
        return [RepoOut(**vars(r)) for r in github.repos(_app_or_404(db), box)]
    except github.GitHubError as e:
        raise _bad_gateway(e) from None


@router.get("/branches")
def list_branches(
    db: DB, box: Box, _user: CurrentUser,
    installation_id: int, repo: str = Query(max_length=200, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"),
) -> list[str]:
    if any(part in (".", "..") for part in repo.split("/")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("github.repo_name_invalid"))
    try:
        return github.branches(_app_or_404(db), box, installation_id, repo)
    except github.GitHubError as e:
        raise _bad_gateway(e) from None


class DatabaseSuggestion(BaseModel):
    engine: str | None
    reason: str | None
    redis: bool


@router.get("/suggest-database")
def suggest_database(
    db: DB, box: Box, _user: CurrentUser,
    installation_id: int, ref: str = Query(max_length=255),
    repo: str = Query(max_length=200, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"),
) -> DatabaseSuggestion:
    """Đọc file dependency/cấu hình của repo để gợi ý loại database cần tạo."""
    from ..detect import SUGGEST_FILES, suggest_database as suggest

    if any(part in (".", "..") for part in repo.split("/")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("github.repo_name_invalid"))
    app = _app_or_404(db)
    try:
        files = {f: text for f in SUGGEST_FILES if (text := github.read_file(app, box, installation_id, repo, f, ref))}
    except github.GitHubError as e:
        raise _bad_gateway(e) from None
    return DatabaseSuggestion(**suggest(files))


class StackDir(BaseModel):
    path: str
    kind: str | None
    framework: str | None
    api_vars: list[str]
    cors_vars: list[str]
    database: str | None
    database_reason: str | None
    redis: bool


@router.get("/inspect")
def inspect_repo(
    db: DB, box: Box, _user: CurrentUser,
    installation_id: int, ref: str = Query(max_length=255),
    repo: str = Query(max_length=200, pattern=r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$"),
) -> list[StackDir]:
    """Các thư mục app trong repo kèm phân loại frontend/backend (cho fullstack / monorepo)."""
    from concurrent.futures import ThreadPoolExecutor

    from .. import stack

    if any(part in (".", "..") for part in repo.split("/")):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("github.repo_name_invalid"))
    app = _app_or_404(db)
    try:
        token = github.installation_token(app, box, installation_id)
        paths = github.tree(token, repo, ref)
        dirs = stack.candidate_dirs(paths)[:20]
        existing = set(paths)
        wanted = [(d, f) for d in dirs for f in stack.READ_FILES if (f"{d}/{f}" if d else f) in existing]
        with ThreadPoolExecutor(max_workers=8) as pool:
            texts = list(pool.map(lambda item: github.read_text(token, repo, f"{item[0]}/{item[1]}" if item[0] else item[1], ref), wanted))
    except github.GitHubError as e:
        raise _bad_gateway(e) from None
    files: dict[str, dict[str, str]] = {d: {} for d in dirs}
    for (d, f), text in zip(wanted, texts):
        if text is not None:
            files[d][f] = text
    return [StackDir(**stack.classify(d, files[d])) for d in dirs]


@router.post("/webhook")
async def app_webhook(
    request: Request,
    db: DB,
    box: Box,
    queue: Queue,
    x_github_event: str = Header(""),
    x_hub_signature_256: str = Header(""),
) -> dict:
    """Webhook chung của App: push vào repo nào thì deploy các service gắn với repo đó."""
    body = await request.body()
    if len(body) > MAX_WEBHOOK_BODY:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, t("webhook.payload_too_large"))
    app = github.get_app(db)
    secret = box.decrypt(app.webhook_secret_enc) if app else ""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not app or not hmac.compare_digest(expected, x_hub_signature_256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, t("webhook.bad_signature"))

    if x_github_event == "ping":
        return {"status": "ok", "message": "pong"}
    if x_github_event != "push":
        return {"status": "ignored", "reason": t("webhook.event_ignored", event=repr(x_github_event))}
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("webhook.not_json")) from None
    if payload.get("deleted"):
        return {"status": "ignored", "reason": t("webhook.branch_deleted")}

    repo_id = (payload.get("repository") or {}).get("id")
    ref = payload.get("ref", "")
    services = db.scalars(select(Service).where(Service.github_repo_id == repo_id)).all() if repo_id else []
    queued = [
        queue.enqueue(db, s, JobKind.DEPLOY, JobTrigger.WEBHOOK).id
        for s in services
        if s.auto_deploy and ref == f"refs/heads/{s.branch}" and github.push_touches(payload, s.root_dir)
    ]
    return {"status": "queued" if queued else "ignored", "job_ids": queued}
