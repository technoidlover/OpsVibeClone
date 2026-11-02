"""Vòng lặp agent: gọi model, chạy tool, dừng chờ duyệt thao tác nguy hiểm.

Mỗi phiên chạy trong một luồng nền. Trạng thái nằm hết trong DB (tin nhắn,
tool call đang chờ, quyết định duyệt), nên giao diện chỉ cần đọc lại định kỳ
và panel khởi động lại vẫn tiếp tục được phiên đang chờ duyệt.
"""

import json
import logging
import shutil
import threading
import time
from collections import defaultdict
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .. import ai
from ..config import Settings
from ..crypto import SecretBox
from ..docker import Docker
from ..host import Sampler
from ..i18n import t
from ..jobs import JobQueue
from ..models import AIProvider, AssistantMessage, AssistantSession, utcnow
from . import agy as agy_cli
from . import attachments, llm, mcp, tools

logger = logging.getLogger(__name__)

# "Model" đặc biệt: Antigravity CLI chạy trên host, dùng tool panel qua MCP (xem assistant/agy.py)
AGY_BRAIN = "cli:agy"

# Số lần gọi model tối đa cho một tin nhắn của người dùng
MAX_TURNS = 40
# Kết quả tool cũ hơn N lượt được rút gọn khi gửi lại cho model, để hội thoại dài không vượt context
KEEP_FULL_RESULTS = 6
OLD_RESULT_CHARS = 1500
DENIED = "The user denied this action."
STOPPED = "Not run: the user pressed Stop."
# Bấm Dừng lúc đang chờ model trả lời: kiểm tra mỗi khoảng này, không chờ model xong
STOP_POLL = 0.5

SYSTEM_PROMPT = """\
You are the DevOps agent of OpsVibes, a self-hosted control panel that runs web apps on a single server \
with Docker + Traefik. You help the user write deploy configuration and debug and fix their websites. \
Be surgical: find the root cause of THE problem at hand with the fewest tool calls, and don't wander.

How the panel works:
- Each service is built from a git repo, an uploaded archive, or runs a prebuilt Docker image. \
Repos build with their Dockerfile if present, otherwise Nixpacks. Apps must listen on 0.0.0.0:$PORT.
- Traefik (container opsvibes-traefik) routes domains to app containers and issues Let's Encrypt certificates.
- Containers share the Docker network "opsvibes"; a service is reachable as <project>-<service>, \
a database by its host name.
- Env vars and build/run config are stored by the panel. They are baked into the container at deploy time, \
so a change takes effect ONLY after deploy_service, which removes the old container and starts a new one. \
After set_env or editing config, you MUST call deploy_service once to apply it — otherwise nothing changes.

Scope — stay focused, this is what keeps you fast and cheap:
- Work on ONE service at a time: the one the user named or the one this conversation is about. \
Do NOT read other projects' or services' logs. Use overview only to find a name you don't have, not every turn.
- Diagnose a service in this order, stopping as soon as you know the cause: \
service_details (state, recent jobs + errors) -> the failing job_log (for build/deploy failures) OR \
container_logs of THAT service's container (for runtime errors) -> http_check its URL if it's a response issue.
- Read logs with a small tail first (e.g. 100-200 lines); only widen if needed. Never dump logs you don't read.

Efficiency:
- Each step is one model request and free keys have tight quotas. Batch the read-only tools you need into one \
turn, and never re-read something you already have.

Creating sites from the chat (the user may attach a source archive and/or a database dump):
- When the user asks to deploy NEW files, do NOT inspect existing projects, services or containers: \
they are unrelated. Call list_attachments first to see what you got and the detected stack. Ask only for what you cannot infer \
(e.g. a custom domain); otherwise pick short lowercase names and the default address.
- Order: create_project (or reuse one) -> create_database if the app needs one (WordPress: mariadb) -> \
import_database with the attached dump (BEFORE the first deploy) -> create_service source=attachment with \
databases=[that database] -> wait_job -> http_check the address.
- The panel configures WordPress itself (wp-config reads the linked database, uploads are kept, a .sql inside the \
archive is imported if the database is still empty): do not edit wp-config for that.
- A separate dump is loaded with import_database; a dump inside the archive can also be loaded with path=.

Fixing:
- Prefer env vars and build/run config over application code. Code edits need approval; prefer telling the user \
the exact file+line to change instead.
- Config/file changes live in your own copy of the source and go live via deploy_service apply_changes=true, \
but are NOT committed to the repo (the next git push won't have them) — tell the user to download the patch to keep it.
- After any fix, apply it with deploy_service, then verify: wait_job, then http_check or container_logs.
- Never print secret values. host_command and exec_in_container need approval: give a clear reason, keep commands \
minimal and non-destructive, and never delete data, volumes or databases unless the user asked.

Reply in {lang}. Be concise. End with: the cause, what you changed, and what the user should do next."""

LANG_NAMES = {"vi": "Vietnamese", "en": "English"}


class BrainError(RuntimeError):
    pass


class _Stopped(Exception):
    """Người dùng bấm Dừng lúc agy đang chạy."""


def resolve_brain(db: Session, box: SecretBox, brain: str, model: str | None) -> llm.Brain:
    kind, _, ref = brain.partition(":")
    if kind != "provider" or not ref.isdigit():
        raise BrainError(t("assistant.brain_unsupported"))
    provider = db.get(AIProvider, int(ref))
    if provider is None or provider.kind not in ai.PROVIDERS:
        raise BrainError(t("ai.provider_not_found"))
    key = box.decrypt_optional(provider.api_key_enc)
    if ai.PROVIDERS[provider.kind].key_required and not key:
        raise BrainError(t("agent.provider_no_key", name=provider.name))
    chosen = model or provider.default_model
    if not chosen:
        raise BrainError(t("assistant.model_required", name=provider.name))
    return llm.brain_for(provider.kind, provider.base_url, key, chosen)


def usable_brains(db: Session, box: SecretBox, primary: str, model: str | None) -> list[tuple[str, llm.Brain]]:
    """Model đã chọn trước, rồi các provider khác có key + model mặc định để dự phòng khi hết quota.

    Khoá chọn (vd gemini-3.8-flash) chỉ dùng cho `primary`; các provider dự phòng dùng model mặc định của chúng.
    """
    out: list[tuple[str, llm.Brain]] = []
    seen: set[str] = set()
    try:
        out.append((primary, resolve_brain(db, box, primary, model)))
        seen.add(primary)
    except BrainError:
        pass
    for p in db.scalars(select(AIProvider).order_by(AIProvider.id)):
        bid = f"provider:{p.id}"
        if bid in seen:
            continue
        try:
            out.append((bid, resolve_brain(db, box, bid, None)))
        except BrainError:
            continue
    if not out:
        resolve_brain(db, box, primary, model)  # không giải được cái nào: ném lỗi gốc để báo rõ
    return out


def complete_fallback(brains: list[tuple[str, llm.Brain]], system: str, messages: list[dict],
                      tools: list[llm.Tool], max_tokens: int = llm.MAX_TOKENS) -> tuple[llm.Reply, llm.Brain]:
    """Gọi lần lượt; model nào hết quota (429) thì tự sang model kế. Lỗi khác thì báo ngay.

    Hết quota ở mọi model: ném lỗi hết quota của model đầu. Trả (reply, brain đã dùng).
    """
    first_rate: llm.RateLimitError | None = None
    for bid, brain in brains:
        try:
            return llm.complete(brain, system, messages, tools, max_tokens), brain
        except llm.RateLimitError as e:
            first_rate = first_rate or e
    raise first_rate  # type: ignore[misc]


def user_blocks(text: str, files: list[dict] | None) -> list[dict]:
    blocks = [{"type": "text", "text": text}] if text.strip() else []
    return blocks + [{"type": "attachment", "name": f["name"], "size": f["size"]} for f in files or []]


def for_model(blocks: list[dict]) -> list[dict]:
    """Khối đính kèm (chỉ để giao diện hiện) thành văn bản cho model."""
    return [{"type": "text", "text": attachments.note(b["name"], b["size"])} if b["type"] == "attachment" else b
            for b in blocks]


def _compact(messages: list[dict]) -> list[dict]:
    """Rút gọn kết quả tool cũ: model vẫn biết đã gọi gì, không phải đọc lại log dài."""
    result_turns = [i for i, m in enumerate(messages) if any(b["type"] == "tool_result" for b in m["content"])]
    old = set(result_turns[:-KEEP_FULL_RESULTS])
    out = []
    for i, m in enumerate(messages):
        if i in old:
            content = []
            for b in m["content"]:
                if b["type"] == "tool_result" and len(b["content"]) > OLD_RESULT_CHARS:
                    b = {**b, "content": b["content"][:OLD_RESULT_CHARS] + "\n[... older output cut ...]"}
                content.append(b)
            m = {**m, "content": content}
        out.append(m)
    return out


class Manager:
    def __init__(self, settings: Settings, sessions: sessionmaker[Session], box: SecretBox,
                 docker: Docker | None, queue: JobQueue, sampler: Sampler | None, lang=lambda: "vi"):
        self.settings = settings
        self.sessions = sessions
        self.box = box
        self.docker = docker
        self.queue = queue
        self.sampler = sampler
        self.lang = lang
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opsvibes-assistant")
        # Gọi model ở luồng riêng để bấm Dừng không phải chờ request (tới vài phút) xong
        self._calls = ThreadPoolExecutor(max_workers=4, thread_name_prefix="opsvibes-assistant-llm")
        self._locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)
        self._stop: set[int] = set()
        # Phiên đang chạy agy: (tên container, token MCP), để bấm Dừng thì kill agy và thu hồi token
        self._agy_runs: dict[int, tuple[str, str]] = {}

    # ---------- Điều khiển từ API ----------

    def recover(self) -> None:
        """Phiên đang chạy dở khi panel tắt: báo lỗi. Phiên đang chờ duyệt giữ nguyên."""
        with self.sessions() as db:
            db.execute(update(AssistantSession).where(AssistantSession.status == "running")
                       .values(status="error", error=t("assistant.interrupted")))
            db.commit()
        self.prune_attachments()

    def prune_attachments(self) -> None:
        """Dọn file đính kèm của các phiên không hoạt động quá attachments.KEEP_DAYS ngày."""
        cutoff = utcnow() - timedelta(days=attachments.KEEP_DAYS)
        with self.sessions() as db:
            ids = db.scalars(select(AssistantSession.id).where(AssistantSession.updated_at < cutoff,
                                                               AssistantSession.status != "running")).all()
        for session_id in ids:
            attachments.remove(self.settings, session_id)

    def start(self, session_id: int) -> None:
        self._stop.discard(session_id)
        self._executor.submit(self._advance_safe, session_id)

    def stop(self, session_id: int, waiting: bool = False) -> None:
        """Dừng sau tool đang chạy. Phiên đang chờ duyệt: từ chối các thao tác còn treo rồi dừng."""
        self._stop.add(session_id)
        run = self._agy_runs.get(session_id)
        if run is not None:
            container, token = run
            mcp.revoke(token)  # agy không gọi thêm được tool nào nữa
            if self.docker is not None:
                threading.Thread(target=self.docker.remove_container, args=(container,), daemon=True).start()
        if waiting:
            self._executor.submit(self._advance_safe, session_id)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._calls.shutdown(wait=False, cancel_futures=True)

    def stopped(self, session_id: int) -> bool:
        return session_id in self._stop

    def _finish_stopped(self, db: Session, session: AssistantSession) -> None:
        self._stop.discard(session.id)
        session.status = "idle"
        db.commit()

    def remove_files(self, session_id: int) -> None:
        shutil.rmtree(self.settings.data_dir / "assistant" / str(session_id), ignore_errors=True)

    @staticmethod
    def add_user_text(db: Session, session: AssistantSession, text: str, files: list[dict] | None = None) -> None:
        """Tin nhắn mới của người dùng (kèm file đã đính kèm). Đang chờ duyệt thì coi như từ chối các thao tác còn treo."""
        blocks = user_blocks(text, files)
        pending = json.loads(session.pending_json) if session.pending_json else None
        if pending:
            for call in pending["calls"]:
                tool = tools.TOOLS.get(call["name"])
                if tool and tool.risk == tools.APPROVE and call["id"] not in pending["decisions"]:
                    pending["decisions"][call["id"]] = {"approve": False, "note": text}
            pending["followup"] = blocks
            session.pending_json = json.dumps(pending)
        else:
            db.add(AssistantMessage(session_id=session.id, role="user", content_json=json.dumps(blocks)))
        session.status = "running"
        session.error = None

    @staticmethod
    def decide(session: AssistantSession, tool_use_id: str, approve: bool, note: str | None) -> bool:
        pending = json.loads(session.pending_json) if session.pending_json else None
        if not pending or not any(c["id"] == tool_use_id for c in pending["calls"]):
            return False
        pending["decisions"][tool_use_id] = {"approve": approve, "note": note}
        session.pending_json = json.dumps(pending)
        session.status = "running"
        return True

    # ---------- Vòng lặp ----------

    def _advance_safe(self, session_id: int) -> None:
        with self._locks[session_id]:
            try:
                self._advance(session_id)
            except Exception as e:  # noqa: BLE001 - lỗi nào cũng phải hiện cho người dùng
                logger.exception("AI Agent phiên %s lỗi", session_id)
                with self.sessions() as db:
                    session = db.get(AssistantSession, session_id)
                    if session:
                        session.status = "error"
                        session.error = str(e)[:2000]
                        db.commit()

    def _advance(self, session_id: int) -> None:
        with self.sessions() as db:
            session = db.get(AssistantSession, session_id)
            if session and session.brain == AGY_BRAIN:
                self._run_agy(db, session)
                return
            ctx = tools.ToolContext(db, self.settings, self.box, self.docker, self.queue, self.sampler, session_id,
                                    sessions=self.sessions, stopped=lambda: self.stopped(session_id))
            for _turn in range(MAX_TURNS):
                session = db.get(AssistantSession, session_id)
                if session is None:
                    return
                if session.pending_json and not self._run_pending(db, session, ctx):
                    return  # chờ người dùng duyệt
                if self.stopped(session_id):
                    self._finish_stopped(db, session)
                    return

                brains = usable_brains(db, self.box, session.brain, session.model)
                # Truy vấn lại mỗi lượt: relationship không tự làm mới (expire_on_commit=False)
                rows = db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == session_id)
                                  .order_by(AssistantMessage.id))
                history = [{"role": m.role, "content": for_model(m.content)} for m in rows]
                call = self._calls.submit(complete_fallback, brains, self._system(session), _compact(history),
                                          [x.spec for x in tools.TOOLS.values()])
                while not call.done():
                    if self.stopped(session_id):
                        # Bỏ câu trả lời đang chờ: hội thoại dừng ở tin nhắn của người dùng
                        call.add_done_callback(lambda f: f.exception())
                        self._finish_stopped(db, db.get(AssistantSession, session_id))
                        return
                    try:
                        call.result(timeout=STOP_POLL)
                    except FutureTimeout:
                        pass
                reply, _brain = call.result()
                session = db.get(AssistantSession, session_id)
                session.input_tokens += reply.usage.get("input", 0) or 0
                session.output_tokens += reply.usage.get("output", 0) or 0
                blocks = reply.blocks or [{"type": "text", "text": t("assistant.no_reply")}]
                db.add(AssistantMessage(session_id=session.id, role="assistant", content_json=json.dumps(blocks)))
                if not reply.tool_calls:
                    session.status = "idle"
                    db.commit()
                    return
                session.pending_json = json.dumps({"calls": reply.tool_calls, "results": [], "decisions": {}})
                db.commit()
            session = db.get(AssistantSession, session_id)
            if session:
                session.status = "error"
                session.error = t("assistant.too_many_turns", max=MAX_TURNS)
                db.commit()

    def _run_agy(self, db: Session, session: AssistantSession) -> None:
        """Chạy Antigravity CLI một lượt cho tin nhắn mới nhất; agy gọi tool panel qua MCP."""
        if self.docker is None:
            session.status, session.error = "error", t("docker.unavailable")
            db.commit()
            return
        # Tin nhắn mới nhất của người dùng, kèm ghi chú file đính kèm (agy chỉ nhận một đoạn văn bản)
        last = next((m for m in reversed(session.messages) if m.role == "user"
                     and any(b["type"] in ("text", "attachment") for b in m.content)), None)
        parts = [b["text"] for b in for_model(last.content) if b["type"] == "text"] if last else []
        files = attachments.summary(self.settings, session.id)
        if files:
            parts.append("Files attached in this conversation (use these exact names): "
                         + ", ".join(f"{f['name']} ({f['size'] / 1024 / 1024:.1f} MB)" for f in files))
        prompt = self._system(session) + "\n\n" + "\n".join(parts)
        token = mcp.grant(session.id)
        container = f"vp_agy_{session.id}_{int(time.time() * 1000)}"
        self._agy_runs[session.id] = (container, token)
        try:
            image = agy_cli.panel_image(self.docker, self.settings.panel_container)
            agy_cli.configure_mcp(self.docker, image, token)
            if self.stopped(session.id):
                raise _Stopped
            text, failed = agy_cli.run(self.docker, image, prompt, session.model,
                                       self.settings.build_timeout_seconds, name=container)
            if self.stopped(session.id):
                raise _Stopped
        except _Stopped:
            db.refresh(session)
            db.add(AssistantMessage(session_id=session.id, role="assistant",
                                    content_json=json.dumps([{"type": "text", "text": t("assistant.stopped")}])))
            self._finish_stopped(db, session)
            return
        except (agy_cli.AgyError, Exception) as e:  # noqa: BLE001 - lỗi nào cũng hiện cho người dùng
            if self.stopped(session.id):
                db.refresh(session)
                self._finish_stopped(db, session)
                return
            session.status, session.error = "error", str(e)[:2000]
            db.commit()
            return
        finally:
            self._agy_runs.pop(session.id, None)
            mcp.revoke(token)
        db.refresh(session)
        db.add(AssistantMessage(session_id=session.id, role="assistant",
                                content_json=json.dumps([{"type": "text", "text": text or t("assistant.no_reply")}])))
        session.status = "error" if failed else "idle"
        session.error = (text or t("assistant.no_reply"))[:2000] if failed else None
        db.commit()

    def _run_pending(self, db: Session, session: AssistantSession, ctx: tools.ToolContext) -> bool:
        """Chạy các tool call còn lại của lượt. Trả False nếu phải dừng chờ duyệt."""
        pending = json.loads(session.pending_json)
        results: list[dict] = pending["results"]
        for call in pending["calls"][len(results):]:
            tool = tools.TOOLS.get(call["name"])
            decision = pending["decisions"].get(call["id"])
            if self.stopped(session.id):
                # Bấm Dừng: không chạy các tool còn lại của lượt (model vẫn cần kết quả cho từng call)
                results.append({"type": "tool_result", "tool_use_id": call["id"], "content": STOPPED, "is_error": True})
                continue
            if tool and decision is None and tools.needs_approval(ctx, tool, call.get("input") or {}):
                session.status = "waiting"
                session.pending_json = json.dumps(pending)
                db.commit()
                return False
            if decision is not None and not decision["approve"]:
                note = decision.get("note")
                content, is_error = DENIED + (f" User's note: {note}" if note else ""), True
            else:
                content, is_error = tools.run(ctx, call["name"], call.get("input") or {})
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": content, "is_error": is_error})
            # Lưu sau mỗi tool để giao diện thấy tiến độ
            session = db.get(AssistantSession, session.id)
            session.pending_json = json.dumps(pending)
            db.commit()

        content = list(results)
        followup = pending.get("followup")
        if isinstance(followup, str):  # phiên lưu từ bản trước
            followup = [{"type": "text", "text": followup}]
        content += followup or []
        db.add(AssistantMessage(session_id=session.id, role="user", content_json=json.dumps(content)))
        session.pending_json = None
        session.status = "running"
        db.commit()
        return True

    def _system(self, session: AssistantSession) -> str:
        prompt = SYSTEM_PROMPT.format(lang=LANG_NAMES.get(self.lang(), "Vietnamese"))
        if session.context_json:
            ctx = json.loads(session.context_json)
            parts = [f"{k}={v}" for k, v in ctx.items() if v is not None]
            prompt += ("\n\nThis conversation is about: " + ", ".join(parts)
                       + ". Work only on this service unless the user clearly asks about something else; "
                       "do not inspect other projects or services.")
        return prompt
