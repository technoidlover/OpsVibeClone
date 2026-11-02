"""Tự sửa khi deploy lỗi: AI Agent đọc log, sửa cấu hình, panel build lại.

Thêm chế độ chuẩn bị (prepare): trước khi build, AI Agent đọc mã nguồn để thêm cấu hình
build/chạy và biến môi trường còn thiếu, đỡ phải đợi lỗi mới sửa.

Chạy ngay trong luồng của job deploy, ghi vào một cuộc hội thoại AI Agent (mỗi
job một cuộc) để người dùng xem lại hoặc chat tiếp. Khác với chat thường:
- File cấu hình sửa thẳng trên mã nguồn của lần deploy đang chạy.
- Không có deploy_service: panel tự build lại khi agent xong.
- Thao tác cần duyệt bị từ chối ngay, để deploy không treo chờ người dùng.
"""

import json

from sqlalchemy import select

from .. import i18n
from ..ai import ProviderError
from ..i18n import t
from ..jobs import JobContext
from ..models import AssistantMessage, AssistantSession
from ..pipelines import load_env
from . import agy as agy_cli
from . import mcp, tools
from .session import AGY_BRAIN, LANG_NAMES, SYSTEM_PROMPT, BrainError, _compact, complete_fallback, usable_brains
from .source import collect_diff, fingerprint

MAX_HEAL_ATTEMPTS = 2
MAX_TURNS = 25
LOG_CONTEXT = 12_000
NO_REDEPLOY = "Not available here: the panel rebuilds automatically when you finish."
NO_APPROVAL = ("Not available during the automatic deploy fix (nobody is there to approve it). If it is needed, "
               "say so in your summary; the user can approve it from the AI Agent chat.")

HEAL_RULES = """

You were started automatically because a deploy failed. Find the cause from the log, then fix it with \
write_config_file (build/run config in this deploy's source) and set_env. Do not call deploy_service: the panel \
rebuilds as soon as you finish. If the cause is in application code or needs a command on the server, do not \
work around it: explain exactly what the user must change. Finish with a short summary."""

PREPARE_RULES = """

You were started automatically before the panel builds this service, so the first deploy works. The source is \
already checked out. Inspect it with list_files/read_file and the current env vars with service_details, then:
- Make sure it can be built (Dockerfile or nixpacks.toml if auto-detection would fail) and that the app listens on \
0.0.0.0:$PORT. Use write_config_file only when something is really missing or wrong.
- Find every env var the app needs at startup (.env.example, config/schema files, getOrThrow/process.env/os.environ \
reads). Set the missing ones with set_env: generate strong random values for secrets (JWT_SECRET, SESSION_SECRET, \
APP_KEY...), reuse the database/redis variables already linked to the service, and pick sane defaults \
(NODE_ENV=production...). Never overwrite a value that is already set.
- Do not invent credentials for third-party services (payment, email, cloud keys...): list them in your summary \
so the user can add them.
Do not call deploy_service and do not edit application code. Finish with a short summary of what you changed."""


def _brief(call: dict) -> str:
    args = call.get("input") or {}
    for key in ("path", "container", "url", "command", "job_id"):
        if key in args:
            return str(args[key]).splitlines()[0][:120] if str(args[key]) else ""
    if call["name"] == "set_env":
        return ", ".join(sorted((args.get("set") or {}).keys()) + [f"-{n}" for n in args.get("unset") or []])
    return ""


def heal(ctx: JobContext, brain_id: str, failure: Exception, attempt: int) -> bool:
    """Trả True nếu agent đã đổi cấu hình (đáng build lại), False nếu không còn gì để thử."""
    service, job = ctx.service, ctx.job
    try:
        tail = ctx.log.path.read_text(errors="replace")[-LOG_CONTEXT:]
    except OSError:
        tail = ""
    prompt = (f"Deploy #{job.id} of {service.project.name}/{service.name} failed (fix attempt {attempt}/"
              f"{MAX_HEAL_ATTEMPTS}). Error: {ctx.log.redact(str(failure))}\n\n--- Deploy log tail ---\n{tail}")
    changed = _run(ctx, brain_id, prompt, HEAL_RULES, "heal.title",
                   lambda model, session: t("heal.start", attempt=attempt, max=MAX_HEAL_ATTEMPTS,
                                            model=model, session=session))
    if changed is None:
        return False
    ctx.log.write(t("heal.rebuild") if changed else t("heal.no_change"))
    return changed


def prepare(ctx: JobContext, brain_id: str) -> None:
    """AI Agent chuẩn bị cấu hình / biến môi trường trước khi build. Lỗi ở đây không chặn deploy."""
    service, job = ctx.service, ctx.job
    root = f" (folder {service.root_dir})" if service.root_dir else ""
    prompt = (f"Deploy #{job.id} of {service.project.name}/{service.name}{root} is about to be built. "
              "Prepare its build/run configuration and env vars.")
    changed = _run(ctx, brain_id, prompt, PREPARE_RULES, "heal.prepare_title",
                   lambda model, session: t("heal.prepare_start", model=model, session=session))
    if changed is not None:
        ctx.log.write(t("heal.prepared") if changed else t("heal.prepare_no_change"))


def _run(ctx: JobContext, brain_id: str, prompt: str, rules: str, title_key: str, start_line) -> bool | None:
    """Chạy agent trên mã nguồn của job. True/False: có đổi file/env hay không; None: agent không chạy được."""
    service, job, db = ctx.service, ctx.job, ctx.db
    is_agy = brain_id == AGY_BRAIN
    brains = None
    if not is_agy:
        try:
            brains = usable_brains(db, ctx.box, brain_id, None)
        except BrainError as e:
            ctx.log.write(t("heal.skipped", error=e))
            return None

    session = db.get(AssistantSession, job.assistant_session_id) if job.assistant_session_id else None
    if session is None:
        target = f"{service.project.name}/{service.name}"
        session = AssistantSession(
            title=t(title_key, job_id=job.id, target=target)[:200], brain=brain_id,
            context_json=json.dumps({"project": service.project.name, "service": service.name, "job_id": job.id}),
        )
        db.add(session)
        db.flush()
        job.assistant_session_id = session.id
    session.status, session.error = "running", None
    db.add(AssistantMessage(session_id=session.id, role="user",
                            content_json=json.dumps([{"type": "text", "text": prompt}])))
    db.commit()
    model_label = "agy" if is_agy else brains[0][1].model
    ctx.log.write(start_line(model_label, session.id))

    system = SYSTEM_PROMPT.format(lang=LANG_NAMES.get(i18n.current(), "Vietnamese")) + rules
    # Chỉ build lại khi file hoặc biến môi trường thật sự khác trước (agent ghi lại y nguyên thì thôi)
    before = (fingerprint(ctx.workspace), load_env(service, ctx.box))
    summary = None
    try:
        if is_agy:
            summary = _agy_heal(ctx, session, system, prompt)
        else:
            summary = _llm_heal(ctx, session, brains, system)
    except (ProviderError, RuntimeError, agy_cli.AgyError) as e:
        session.status, session.error = "error", str(e)[:2000]
        job.result = summary
        db.commit()
        ctx.log.write(t("heal.failed", error=ctx.log.redact(str(e))))
        return None

    session.status = "idle"
    job.result = ctx.log.redact(summary) if summary else None
    job.diff = ctx.log.redact(collect_diff(ctx.workspace, ctx.settings.patch_path(job.id))) or None
    db.commit()
    if summary:
        first = summary.strip().splitlines()[0].replace("**", "").lstrip("#- ").strip()
        ctx.log.write(t("heal.summary", text=ctx.log.redact(first)[:300]))
    db.refresh(service)
    return (fingerprint(ctx.workspace), load_env(service, ctx.box)) != before


def _llm_heal(ctx: JobContext, session: AssistantSession, brains, system: str) -> str | None:
    """Vòng lặp tự sửa bằng model API (gọi tool có duyệt-tự-từ-chối, panel tự build lại)."""
    db = ctx.db
    specs = [x.spec for name, x in tools.TOOLS.items() if name != "deploy_service" and name not in tools.CHAT_ONLY]
    tool_ctx = tools.ToolContext(db, ctx.settings, ctx.box, ctx._docker, None, None, session.id,
                                 heal_service_id=ctx.service.id, heal_workspace=ctx.workspace)
    summary = None
    for _turn in range(MAX_TURNS):
        rows = db.scalars(select(AssistantMessage).where(AssistantMessage.session_id == session.id)
                          .order_by(AssistantMessage.id))
        reply, _brain = complete_fallback(brains, system,
                                          _compact([{"role": m.role, "content": m.content} for m in rows]), specs)
        session.input_tokens += reply.usage.get("input", 0) or 0
        session.output_tokens += reply.usage.get("output", 0) or 0
        blocks = reply.blocks or [{"type": "text", "text": t("assistant.no_reply")}]
        db.add(AssistantMessage(session_id=session.id, role="assistant", content_json=json.dumps(blocks)))
        texts = [b["text"] for b in blocks if b["type"] == "text" and b["text"].strip()]
        summary = texts[-1] if texts else summary
        if not reply.tool_calls:
            break
        results = []
        for call in reply.tool_calls:
            tool = tools.TOOLS.get(call["name"])
            if call["name"] == "deploy_service":
                content, is_error = NO_REDEPLOY, True
            elif tool and (tool.risk == tools.APPROVE or call["name"] in tools.CHAT_ONLY):
                content, is_error = NO_APPROVAL, True
            else:
                content, is_error = tools.run(tool_ctx, call["name"], call.get("input") or {})
            ctx.log.write(f"    [{call['name']}] {_brief(call)}" + (" ✗" if is_error else ""))
            results.append({"type": "tool_result", "tool_use_id": call["id"], "content": content, "is_error": is_error})
        db.add(AssistantMessage(session_id=session.id, role="user", content_json=json.dumps(results)))
        db.commit()
    else:
        raise RuntimeError(t("assistant.too_many_turns", max=MAX_TURNS))
    return summary


def _agy_heal(ctx: JobContext, session: AssistantSession, system: str, prompt: str) -> str | None:
    """Tự sửa bằng Antigravity CLI: agy sửa cấu hình qua MCP của panel (chỉ tool đọc + sửa cấu hình)."""
    docker = ctx._docker
    if docker is None:
        raise agy_cli.AgyError(t("docker.unavailable"))
    token = mcp.grant(session.id, heal_service_id=ctx.service.id, heal_workspace=ctx.workspace)
    try:
        image = agy_cli.panel_image(docker, ctx.settings.panel_container)
        agy_cli.configure_mcp(docker, image, token)
        text, failed = agy_cli.run(docker, image, f"{system}\n\n{prompt}", None, ctx.settings.build_timeout_seconds)
    finally:
        mcp.revoke(token)
    ctx.db.add(AssistantMessage(session_id=session.id, role="assistant",
                                content_json=json.dumps([{"type": "text", "text": text or t("assistant.no_reply")}])))
    ctx.db.commit()
    if failed:
        raise RuntimeError(text or t("assistant.no_reply"))
    return text
