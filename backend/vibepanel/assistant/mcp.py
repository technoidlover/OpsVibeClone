"""MCP server nội bộ để agy (Antigravity CLI) dùng bộ tool của panel.

agy chạy trên host, kết nối tới đây như một MCP server HTTP. Chỉ expose tool đọc +
tự động (không có tool cần duyệt) nên agy không chạm được thao tác nguy hiểm — "giữ
duyệt" được bảo đảm bằng việc đơn giản là không đưa các tool đó cho agy.

Mỗi lần chạy agy, panel cấp một token riêng ngắn hạn (grant) và thu hồi sau khi xong
(revoke). Endpoint chỉ nhận token đó (Bearer), không dùng cookie đăng nhập; token ngẫu
nhiên nên dù endpoint có public qua Traefik cũng không gọi khống được.

Từng tool agy gọi được ghi vào cuộc hội thoại để người dùng xem lại trong trang AI Agent.
"""

import json
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Request, Response
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ..models import AssistantMessage
from . import tools

PROTOCOL_VERSION = "2025-06-18"
# Tool đưa cho agy: chỉ đọc + tự động (không có host_command / exec_in_container / write_source_file).
# import_database (cần duyệt khi ghi đè) chỉ chạy được khi không cần duyệt: agy không có bước chờ duyệt.
AGENT_TOOL_NAMES = [name for name, spec in tools.TOOLS.items()
                    if spec.risk in (tools.READ, tools.AUTO) or spec.auto_if is not None]
# Khi tự sửa lúc deploy: panel tự build lại, nên không cho agy tự deploy, chờ job hay tạo mới
HEAL_EXCLUDE = {"deploy_service", "wait_job"} | tools.CHAT_ONLY
NEEDS_APPROVAL = ("Not run: this needs the user's approval, which is not available with Antigravity CLI. "
                  "Tell the user to approve it with an API model in the panel chat, or do it on the database page.")


def _names_for(binding: "Binding") -> list[str]:
    if binding.heal_service_id is not None:
        return [n for n in AGENT_TOOL_NAMES if n not in HEAL_EXCLUDE]
    return AGENT_TOOL_NAMES

router = APIRouter(prefix="/api/assistant/mcp", tags=["mcp"])


@dataclass
class Binding:
    session_id: int
    expires: float
    heal_service_id: int | None = None
    heal_workspace: Path | None = None


_tokens: dict[str, Binding] = {}
_lock = threading.Lock()


def grant(session_id: int, ttl: float = 3600, heal_service_id: int | None = None,
          heal_workspace: Path | None = None) -> str:
    token = secrets.token_urlsafe(32)
    with _lock:
        _tokens[token] = Binding(session_id, time.monotonic() + ttl, heal_service_id, heal_workspace)
    return token


def revoke(token: str) -> None:
    with _lock:
        _tokens.pop(token, None)


def _binding(token: str | None) -> Binding | None:
    if not token:
        return None
    with _lock:
        b = _tokens.get(token)
        if b is None:
            return None
        if b.expires <= time.monotonic():
            _tokens.pop(token, None)
            return None
        return b


def _tool_defs(names: list[str]) -> list[dict]:
    return [{"name": n, "description": tools.TOOLS[n].description, "inputSchema": tools.TOOLS[n].schema}
            for n in names]


def _context(request: Request, db: Session, b: Binding) -> tools.ToolContext:
    app = request.app.state
    return tools.ToolContext(db, app.settings, app.box, app.docker, app.queue, app.sampler, b.session_id,
                             heal_service_id=b.heal_service_id, heal_workspace=b.heal_workspace,
                             sessions=app.sessions)


def _log_call(db: Session, session_id: int, name: str, args: dict, result: str, is_error: bool) -> None:
    """Ghi tool agy vừa gọi vào hội thoại (một block tool_use + một tool_result) để hiển thị lại."""
    call_id = "agy_" + secrets.token_hex(6)
    db.add(AssistantMessage(session_id=session_id, role="assistant",
                            content_json=json.dumps([{"type": "tool_use", "id": call_id, "name": name, "input": args}])))
    db.add(AssistantMessage(session_id=session_id, role="user",
                            content_json=json.dumps([{"type": "tool_result", "tool_use_id": call_id,
                                                      "content": result, "is_error": is_error}])))
    db.commit()


def _ok(req_id, result):
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id, code, message):
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


@router.post("")
async def rpc(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else None
    binding = _binding(token)

    try:
        message = json.loads(await request.body())
    except ValueError:
        return _json(_err(None, -32700, "Parse error"))
    if not isinstance(message, dict):
        return _json(_err(None, -32600, "Invalid Request"))

    method = message.get("method")
    req_id = message.get("id")
    # Notification (không có id): chỉ xác nhận
    if req_id is None and method and method.startswith("notifications/"):
        return Response(status_code=202)

    if binding is None:
        return _json(_err(req_id, -32001, "Unauthorized"), status=401)

    if method == "initialize":
        return _json(_ok(req_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "opsvibes", "version": request.app.version},
        }))
    if method == "ping":
        return _json(_ok(req_id, {}))
    if method == "tools/list":
        return _json(_ok(req_id, {"tools": _tool_defs(_names_for(binding))}))
    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in _names_for(binding):
            return _json(_ok(req_id, {"content": [{"type": "text", "text": f"Tool {name} is not available here"}],
                                      "isError": True}))
        sessions = request.app.state.sessions

        def call() -> tuple[str, bool]:
            with sessions() as db:
                ctx = _context(request, db, binding)
                if tools.needs_approval(ctx, tools.TOOLS[name], args):
                    content, is_error = NEEDS_APPROVAL, True
                else:
                    content, is_error = tools.run(ctx, name, args)
                _log_call(db, binding.session_id, name, args, content, is_error)
            return content, is_error

        # Tool có thể chạy lâu (import database, chờ deploy): không chặn các request khác của panel
        content, is_error = await run_in_threadpool(call)
        return _json(_ok(req_id, {"content": [{"type": "text", "text": content}], "isError": is_error}))

    return _json(_err(req_id, -32601, f"Method not found: {method}"))


def _json(payload: dict, status: int = 200) -> Response:
    return Response(content=json.dumps(payload), media_type="application/json", status_code=status)
