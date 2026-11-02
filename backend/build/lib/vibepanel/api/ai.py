"""AI provider (API key) cho AI Agent. AI là tùy chọn: panel deploy bình thường khi
chưa cấu hình gì ở đây. Mọi AI đều chạy bằng API key, không đăng nhập tài khoản cá
nhân (điều khoản của nhà cung cấp cấm phần mềm bên thứ ba)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import ai
from ..assistant import agy as agy_cli
from ..assistant import llm
from ..docker import DockerError
from ..i18n import t
from ..models import AIProvider
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep

router = APIRouter(prefix="/api/ai", tags=["ai"])


class ProviderSpecOut(BaseModel):
    kind: str
    label: str
    base_url: str
    key_url: str | None
    key_required: bool


class ProviderOut(BaseModel):
    id: int
    kind: str
    label: str
    name: str
    base_url: str
    has_key: bool
    # 4 ký tự cuối để người dùng nhận ra key nào đang lưu
    key_hint: str | None
    default_model: str | None
    updated_at: datetime


def _base_url(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().rstrip("/")
    if value and not value.startswith(("http://", "https://")):
        raise ValueError(t("ai.base_url_scheme"))
    return value


class ProviderCreate(BaseModel):
    kind: str
    name: str | None = Field(None, max_length=60)
    base_url: str | None = Field(None, max_length=500)
    api_key: str | None = Field(None, max_length=1000)
    default_model: str | None = Field(None, max_length=200)

    _url = field_validator("base_url")(_base_url)


class ProviderUpdate(BaseModel):
    name: str | None = Field(None, max_length=60)
    base_url: str | None = Field(None, max_length=500)
    # Bỏ trống = giữ key cũ; chuỗi rỗng = xóa key
    api_key: str | None = Field(None, max_length=1000)
    default_model: str | None = Field(None, max_length=200)

    _url = field_validator("base_url")(_base_url)


class KeyCheck(BaseModel):
    """Kiểm tra key/model trước khi lưu. provider_id: dùng key đã lưu khi không nhập key mới."""

    kind: str | None = None
    provider_id: int | None = None
    base_url: str | None = Field(None, max_length=500)
    api_key: str | None = Field(None, max_length=1000)
    model: str | None = Field(None, max_length=200)

    _url = field_validator("base_url")(_base_url)


def provider_out(p: AIProvider, box: Box) -> ProviderOut:
    key = box.decrypt_optional(p.api_key_enc)
    return ProviderOut(
        id=p.id,
        kind=p.kind,
        label=ai.PROVIDERS[p.kind].display_label if p.kind in ai.PROVIDERS else p.kind,
        name=p.name,
        base_url=p.base_url,
        has_key=bool(key),
        key_hint=key[-4:] if key and len(key) >= 12 else None,
        default_model=p.default_model,
        updated_at=p.updated_at,
    )


def _clean(value: str | None) -> str | None:
    return value.strip() or None if value is not None else None


def _load_provider(db: DB, provider_id: int) -> AIProvider:
    p = db.get(AIProvider, provider_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("ai.provider_not_found"))
    return p


def _commit(db: DB) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, t("ai.provider_name_taken")) from None


@router.get("/catalog")
def catalog(_user: CurrentUser) -> list[ProviderSpecOut]:
    return [ProviderSpecOut(**{k: getattr(s, k) for k in ProviderSpecOut.model_fields} | {"label": s.display_label})
            for s in ai.PROVIDERS.values()]


@router.get("/providers")
def list_providers(db: DB, box: Box, _user: CurrentUser) -> list[ProviderOut]:
    return [provider_out(p, box) for p in db.scalars(select(AIProvider).order_by(AIProvider.id))]


@router.post("/providers", status_code=status.HTTP_201_CREATED)
def create_provider(req: ProviderCreate, db: DB, box: Box, _user: CurrentUser) -> ProviderOut:
    spec = ai.PROVIDERS.get(req.kind)
    if spec is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.provider_unsupported"))
    base_url = req.base_url or spec.base_url
    if not base_url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.base_url_required"))
    key = _clean(req.api_key)
    if spec.key_required and not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.api_key_required"))
    p = AIProvider(
        kind=spec.kind,
        name=_clean(req.name) or spec.display_label,
        base_url=base_url,
        api_key_enc=box.encrypt_optional(key),
        default_model=_clean(req.default_model),
    )
    db.add(p)
    _commit(db)
    return provider_out(p, box)


@router.patch("/providers/{provider_id}")
def update_provider(provider_id: int, req: ProviderUpdate, db: DB, box: Box, _user: CurrentUser) -> ProviderOut:
    p = _load_provider(db, provider_id)
    fields = req.model_fields_set
    if "name" in fields and _clean(req.name):
        p.name = _clean(req.name)
    if "base_url" in fields:
        p.base_url = req.base_url or ai.PROVIDERS[p.kind].base_url
        if not p.base_url:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.base_url_required"))
    if req.api_key is not None:
        key = _clean(req.api_key)
        if not key and ai.PROVIDERS[p.kind].key_required:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.api_key_mandatory"))
        p.api_key_enc = box.encrypt_optional(key)
    if "default_model" in fields:
        p.default_model = _clean(req.default_model)
    _commit(db)
    return provider_out(p, box)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_provider(provider_id: int, db: DB, _user: CurrentUser) -> None:
    db.delete(_load_provider(db, provider_id))
    db.commit()


@router.post("/providers/{provider_id}/models")
def provider_models(provider_id: int, db: DB, box: Box, _user: CurrentUser) -> list[str]:
    """Kiểm tra key bằng cách gọi API liệt kê model của provider."""
    p = _load_provider(db, provider_id)
    key = box.decrypt_optional(p.api_key_enc)
    try:
        return ai.list_models(p.kind, p.base_url, key)
    except ai.ProviderError as e:
        message = str(e).replace(key, "***") if key else str(e)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, message) from None


def _check_target(req: KeyCheck, db: DB, box: Box) -> tuple[str, str, str | None]:
    saved = _load_provider(db, req.provider_id) if req.provider_id is not None else None
    kind = saved.kind if saved else req.kind
    spec = ai.PROVIDERS.get(kind or "")
    if spec is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.provider_unsupported"))
    base_url = req.base_url or (saved.base_url if saved else spec.base_url)
    if not base_url:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.base_url_required"))
    key = _clean(req.api_key) or (box.decrypt_optional(saved.api_key_enc) if saved else None)
    if spec.key_required and not key:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.api_key_required"))
    return spec.kind, base_url, key


def _provider_error(e: Exception, key: str | None) -> HTTPException:
    message = str(e).replace(key, "***") if key else str(e)
    return HTTPException(status.HTTP_502_BAD_GATEWAY, message)


@router.post("/check/models")
def check_models(req: KeyCheck, db: DB, box: Box, _user: CurrentUser) -> list[str]:
    """Model chat/gọi tool được mà key này dùng (chưa cần lưu provider)."""
    kind, base_url, key = _check_target(req, db, box)
    try:
        return ai.list_models(kind, base_url, key)
    except ai.ProviderError as e:
        raise _provider_error(e, key) from None


@router.post("/check/model")
def check_model(req: KeyCheck, db: DB, box: Box, _user: CurrentUser) -> dict[str, bool]:
    """Gửi thử một request nhỏ có kèm tool: biết model có dùng được với key này không (quota, quyền, tool calling)."""
    kind, base_url, key = _check_target(req, db, box)
    model = _clean(req.model)
    if not model:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("ai.model_required"))
    probe = llm.Tool("ping", "Connectivity test. Do not call it.", {"type": "object", "properties": {}, "required": []})
    try:
        llm.complete(llm.brain_for(kind, base_url, key, model), "Reply with the single word OK.",
                     [{"role": "user", "content": [{"type": "text", "text": "ping"}]}], [probe], max_tokens=16)
    except ai.ProviderError as e:
        raise _provider_error(e, key) from None
    return {"ok": True}


# ---------- Antigravity CLI (agy) trên máy chủ ----------
# agy đăng nhập bằng tài khoản Google. ToS của Google có thể khóa tài khoản khi phần mềm
# bên thứ ba điều khiển agy bằng tài khoản cá nhân: giao diện hiện cảnh báo, người dùng tự quyết.

class AgyStatusOut(BaseModel):
    installed: bool
    connected: bool
    version: str | None
    email: str | None
    logging_in: bool
    # Lệnh người dùng tự chạy qua SSH nếu luồng đăng nhập trên giao diện không nhận mã
    ssh_login_hint: str


class AgyLoginOut(BaseModel):
    url: str | None
    code: str | None
    output: str


class AgyCode(BaseModel):
    code: str = Field(min_length=2, max_length=200)


def _agy_docker(docker: DockerDep):
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    return docker


def _agy_image(docker, settings: AppSettings) -> str:
    try:
        return agy_cli.panel_image(docker, settings.panel_container)
    except agy_cli.AgyError as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(e)) from None


def _agy_status(docker, settings: AppSettings, with_version: bool = True) -> AgyStatusOut:
    st = agy_cli.status(docker, _agy_image(docker, settings), settings.data_dir, with_version)
    return AgyStatusOut(installed=st.installed, connected=st.connected, version=st.version, email=st.email,
                        logging_in=st.logging_in, ssh_login_hint=agy_cli.SSH_LOGIN_HINT)


def _agy_guard(fn):
    try:
        return fn()
    except agy_cli.AgyError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None


@router.get("/agy")
def agy_status(docker: DockerDep, settings: AppSettings, _user: CurrentUser, quick: bool = False) -> AgyStatusOut:
    """quick=1: chỉ đọc file (không gọi agy --version), dùng khi poll chờ đăng nhập."""
    return _agy_status(_agy_docker(docker), settings, with_version=not quick)


@router.post("/agy/install")
def agy_install(docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> AgyStatusOut:
    docker = _agy_docker(docker)
    _agy_guard(lambda: agy_cli.install(docker, _agy_image(docker, settings), settings.build_timeout_seconds))
    return _agy_status(docker, settings)


@router.delete("/agy", status_code=status.HTTP_204_NO_CONTENT)
def agy_uninstall(docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> None:
    docker = _agy_docker(docker)
    _agy_guard(lambda: agy_cli.uninstall(docker, _agy_image(docker, settings)))


@router.post("/agy/login")
def agy_login(docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> AgyLoginOut:
    docker = _agy_docker(docker)
    prompt = _agy_guard(lambda: agy_cli.login_start(docker, _agy_image(docker, settings), settings.data_dir))
    return AgyLoginOut(url=prompt.url, code=prompt.code, output=prompt.output)


@router.post("/agy/login/code")
def agy_login_code(req: AgyCode, docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> AgyStatusOut:
    docker = _agy_docker(docker)
    st = _agy_guard(lambda: agy_cli.login_submit(docker, _agy_image(docker, settings), settings.data_dir, req.code))
    return AgyStatusOut(installed=st.installed, connected=st.connected, version=st.version, email=st.email,
                        logging_in=st.logging_in, ssh_login_hint=agy_cli.SSH_LOGIN_HINT)


@router.delete("/agy/login", status_code=status.HTTP_204_NO_CONTENT)
def agy_login_cancel(docker: DockerDep, _user: CurrentUser) -> None:
    agy_cli.login_cancel(_agy_docker(docker))


@router.delete("/agy/account", status_code=status.HTTP_204_NO_CONTENT)
def agy_logout(docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> None:
    docker = _agy_docker(docker)
    _agy_guard(lambda: agy_cli.logout(docker, _agy_image(docker, settings)))
