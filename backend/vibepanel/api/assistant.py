"""AI Agent cho cả máy chủ: hội thoại, duyệt thao tác nguy hiểm."""

import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import ai, archive
from ..assistant import agy as agy_cli
from ..assistant import attachments, tools
from ..assistant.session import AGY_BRAIN, BrainError, Manager, resolve_brain, user_blocks
from ..i18n import t
from ..models import AIProvider, AssistantMessage, AssistantSession
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _manager(request: Request) -> Manager:
    return request.app.state.assistant


class BrainOut(BaseModel):
    # Giá trị lưu vào phiên: "provider:<id>"
    id: str
    label: str
    default_model: str | None


class SessionCreate(BaseModel):
    brain: str = Field(max_length=40)
    model: str | None = Field(None, max_length=200)
    message: str = Field("", max_length=20_000)
    # Token của file đã tải lên qua POST /uploads
    attachments: list[str] = Field(default_factory=list, max_length=10)
    project: str | None = Field(None, max_length=40)
    service: str | None = Field(None, max_length=40)
    job_id: int | None = None


class MessageIn(BaseModel):
    text: str = Field("", max_length=20_000)
    attachments: list[str] = Field(default_factory=list, max_length=10)
    # Đổi model giữa chừng (vd model rẻ để đọc log, model mạnh để sửa)
    brain: str | None = Field(None, max_length=40)
    model: str | None = Field(None, max_length=200)


class UploadOut(BaseModel):
    token: str
    name: str
    size: int


class DecisionIn(BaseModel):
    tool_use_id: str = Field(max_length=200)
    approve: bool
    note: str | None = Field(None, max_length=2000)


class SessionOut(BaseModel):
    id: int
    title: str
    brain: str
    model: str | None
    status: str
    error: str | None
    context: dict | None
    input_tokens: int
    output_tokens: int
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    id: int
    role: str
    content: list[dict]
    created_at: datetime


class AttachmentOut(BaseModel):
    name: str
    size: int


class SessionDetail(SessionOut):
    messages: list[MessageOut]
    # File đính kèm còn trên đĩa (đã xóa thì tin nhắn vẫn hiện tên nhưng file không còn)
    attachments: list[AttachmentOut]
    # Lượt đang chạy: tool call, kết quả đã có, quyết định duyệt
    pending: dict | None


def _out(s: AssistantSession) -> SessionOut:
    return SessionOut(
        id=s.id, title=s.title, brain=s.brain, model=s.model, status=s.status, error=s.error,
        context=json.loads(s.context_json) if s.context_json else None,
        input_tokens=s.input_tokens, output_tokens=s.output_tokens, created_at=s.created_at, updated_at=s.updated_at,
    )


def _load(db: DB, session_id: int) -> AssistantSession:
    s = db.get(AssistantSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("assistant.session_not_found"))
    return s


def _agy_connected(docker, settings: AppSettings) -> bool:
    if docker is None:
        return False
    try:
        return agy_cli.status(docker, agy_cli.panel_image(docker, settings.panel_container),
                              settings.data_dir, with_version=False).connected
    except (agy_cli.AgyError, Exception):  # noqa: BLE001 - lỗi docker/host không được chặn việc chọn model
        return False


def _check_brain(db: DB, box: Box, brain: str, model: str | None, docker=None, settings=None) -> None:
    if brain == AGY_BRAIN:
        if not _agy_connected(docker, settings):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("assistant.agy_not_connected"))
        return
    try:
        resolve_brain(db, box, brain, model)
    except BrainError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None


@router.get("/brains")
def list_brains(db: DB, box: Box, docker: DockerDep, settings: AppSettings, _user: CurrentUser) -> list[BrainOut]:
    out = []
    for p in db.scalars(select(AIProvider).order_by(AIProvider.id)):
        spec = ai.PROVIDERS.get(p.kind)
        if spec is None or (spec.key_required and not box.decrypt_optional(p.api_key_enc)):
            continue
        out.append(BrainOut(id=f"provider:{p.id}", label=p.name, default_model=p.default_model))
    if _agy_connected(docker, settings):
        out.append(BrainOut(id=AGY_BRAIN, label="Antigravity CLI", default_model=None))
    return out


@router.get("/tools")
def list_tools(_user: CurrentUser) -> dict[str, str]:
    """Mức quyền của từng tool, để giao diện biết call nào cần nút duyệt."""
    return {name: tool.risk for name, tool in tools.TOOLS.items()}


@router.get("/sessions")
def list_sessions(db: DB, _user: CurrentUser) -> list[SessionOut]:
    rows = db.scalars(select(AssistantSession).order_by(AssistantSession.updated_at.desc()).limit(100))
    return [_out(s) for s in rows]


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
def create_session(req: SessionCreate, request: Request, db: DB, box: Box, docker: DockerDep,
                   settings: AppSettings, _user: CurrentUser) -> SessionOut:
    model = (req.model or "").strip() or None
    _check_brain(db, box, req.brain, model, docker, settings)
    context = {k: v for k, v in {"project": req.project, "service": req.service, "job_id": req.job_id}.items() if v}
    if not req.message.strip() and not req.attachments:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("assistant.empty_message"))
    s = AssistantSession(
        title="", brain=req.brain, model=model, status="running",
        context_json=json.dumps(context) if context else None,
    )
    db.add(s)
    db.flush()
    files = attachments.claim(settings, s.id, req.attachments)
    first_line = (req.message.strip().splitlines() or [""])[0] or ", ".join(f["name"] for f in files)
    s.title = first_line[:120] or "…"
    db.add(AssistantMessage(session_id=s.id, role="user", content_json=json.dumps(user_blocks(req.message, files))))
    db.commit()
    _manager(request).start(s.id)
    return _out(s)


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: DB, settings: AppSettings, _user: CurrentUser, after: int = 0) -> SessionDetail:
    """after: chỉ trả tin nhắn có id lớn hơn (giao diện đọc tăng dần)."""
    s = _load(db, session_id)
    rows = db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == s.id, AssistantMessage.id > after)
                      .order_by(AssistantMessage.id))
    return SessionDetail(
        **_out(s).model_dump(),
        messages=[MessageOut(id=m.id, role=m.role, content=m.content, created_at=m.created_at) for m in rows],
        pending=json.loads(s.pending_json) if s.pending_json else None,
        attachments=[AttachmentOut(**a) for a in attachments.summary(settings, s.id)],
    )


@router.post("/sessions/{session_id}/messages", status_code=status.HTTP_202_ACCEPTED)
def send_message(session_id: int, req: MessageIn, request: Request, db: DB, box: Box, docker: DockerDep,
                 settings: AppSettings, _user: CurrentUser) -> SessionOut:
    s = _load(db, session_id)
    if s.status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("assistant.busy"))
    if not req.text.strip() and not req.attachments:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("assistant.empty_message"))
    if req.brain is not None:
        model = (req.model or "").strip() or None
        _check_brain(db, box, req.brain, model, docker, settings)
        s.brain, s.model = req.brain, model
    Manager.add_user_text(db, s, req.text, attachments.claim(settings, s.id, req.attachments))
    db.commit()
    _manager(request).start(s.id)
    return _out(s)


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def upload_attachment(request: Request, settings: AppSettings, _user: CurrentUser,
                            filename: str = Query(max_length=255)) -> UploadOut:
    """Tải file đính kèm (thân request là nội dung file) trước khi gửi tin nhắn; trả token để gửi kèm."""
    _manager(request).prune_attachments()
    try:
        token, target = attachments.new_staging(settings, filename)
    except attachments.AttachmentError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            t("assistant.attachment_type", types=", ".join(attachments.SUFFIXES))) from None
    size = checked = 0
    try:
        archive.check_space(target.parent, int(request.headers.get("content-length") or 0))
        with target.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > attachments.MAX_UPLOAD:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                                        t("assistant.attachment_too_large", limit=attachments.MAX_UPLOAD // archive.GB))
                if size - checked >= 256 * 1024 ** 2:
                    checked = size
                    archive.check_space(target.parent, 0)
                out.write(chunk)
        if size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.upload_empty"))
    except archive.ArchiveError as e:
        target.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e)) from None
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return UploadOut(token=token, name=target.name, size=size)


@router.delete("/uploads/{token}", status_code=status.HTTP_204_NO_CONTENT)
def discard_upload(token: str, settings: AppSettings, _user: CurrentUser) -> None:
    attachments.discard_staged(settings, token)


@router.delete("/sessions/{session_id}/attachments", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachments(session_id: int, db: DB, settings: AppSettings, _user: CurrentUser,
                       name: str | None = Query(None, max_length=255)) -> None:
    """Xóa một file đính kèm (name) hoặc tất cả; agent đang chạy thì chờ, vì có thể đang dùng file."""
    s = _load(db, session_id)
    if s.status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("assistant.busy"))
    try:
        attachments.remove(settings, s.id, name)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("assistant.attachment_not_found")) from None


@router.post("/sessions/{session_id}/decisions", status_code=status.HTTP_202_ACCEPTED)
def decide(session_id: int, req: DecisionIn, request: Request, db: DB, _user: CurrentUser) -> SessionOut:
    s = _load(db, session_id)
    if s.status != "waiting" or not Manager.decide(s, req.tool_use_id, req.approve, req.note):
        raise HTTPException(status.HTTP_409_CONFLICT, t("assistant.nothing_to_decide"))
    db.commit()
    _manager(request).start(s.id)
    return _out(s)


@router.post("/sessions/{session_id}/stop", status_code=status.HTTP_202_ACCEPTED)
def stop(session_id: int, request: Request, db: DB, _user: CurrentUser) -> SessionOut:
    s = _load(db, session_id)
    waiting = s.status == "waiting"
    if waiting:
        s.status = "running"
        db.commit()
    _manager(request).stop(s.id, waiting)
    return _out(s)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(session_id: int, request: Request, db: DB, _user: CurrentUser) -> None:
    s = _load(db, session_id)
    if s.status == "running":
        raise HTTPException(status.HTTP_409_CONFLICT, t("assistant.busy"))
    db.delete(s)
    db.commit()
    _manager(request).remove_files(session_id)
