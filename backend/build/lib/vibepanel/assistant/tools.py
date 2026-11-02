"""Bộ tool của AI Agent, dùng chung cho model qua API và CLI (qua MCP).

Mỗi tool có một mức quyền, kiểm tra ở server (không dựa vào prompt):
- read:    chỉ đọc (log, trạng thái, file) — chạy ngay.
- auto:    thay đổi cấu hình có thể làm lại (file cấu hình, biến môi trường,
           restart, deploy) — chạy ngay, hiện trong hội thoại.
- approve: lệnh tùy ý trên máy chủ/container, sửa mã nguồn — chờ người dùng duyệt. Tool có `auto_if`
           chạy ngay khi điều kiện an toàn đúng (vd import vào database còn trống).
Tạo project / database / service và import database (CHAT_ONLY) chỉ dùng trong khung chat,
với file người dùng đính kèm (attachments.py).

Mô tả tool và kết quả trả về viết cho model đọc (tiếng Anh), không hiện trực
tiếp thành giao diện.
"""

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .. import pipelines
from ..config import Settings
from ..crypto import SecretBox
from ..docker import Docker, DockerError
from ..host import Sampler
from ..jobs import JobQueue
from ..models import Database, Job, JobKind, JobStatus, JobTrigger, Project, Service, utcnow
from . import attachments
from .llm import Tool
from .source import collect_diff, is_config_file

READ, AUTO, APPROVE = "read", "auto", "approve"
MAX_OUTPUT = 20_000
MAX_FILE = 200_000
EXEC_TIMEOUT = 120
MAX_EXEC_TIMEOUT = 600
MAX_WAIT = 900


class ToolError(Exception):
    """Lỗi trả về cho model như kết quả tool (is_error), không làm hỏng phiên."""


@dataclass
class ToolContext:
    db: Session
    settings: Settings
    box: SecretBox
    docker: Docker | None
    # None khi tự sửa lúc deploy (panel tự build lại, không xếp deploy mới)
    queue: JobQueue | None
    sampler: Sampler | None
    session_id: int
    # Tự sửa lúc deploy: sửa thẳng mã nguồn của lần deploy đang chạy để panel build lại
    heal_service_id: int | None = None
    heal_workspace: Path | None = None
    # Cần cho việc chạy nền (tạo / import database); None ở các nơi không dùng tới
    sessions: sessionmaker[Session] | None = None
    # Người dùng đã bấm Dừng: tool chờ lâu (wait_job) thoát sớm
    stopped: Callable[[], bool] = lambda: False

    def need_docker(self) -> Docker:
        if self.docker is None:
            raise ToolError("Docker is not available to the panel on this machine")
        return self.docker


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    schema: dict
    risk: str
    handler: Callable[[ToolContext, dict], str]
    # Tool APPROVE: điều kiện chạy ngay không cần duyệt
    auto_if: Callable[[ToolContext, dict], bool] | None = None

    @property
    def spec(self) -> Tool:
        return Tool(self.name, self.description, self.schema)


def _tail(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else f"[... {len(text) - limit} earlier characters cut ...]\n" + text[-limit:]


def _obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties, "required": required or []}


_STR = {"type": "string"}
_SERVICE_ARGS = {
    "project": {"type": "string", "description": "Project name"},
    "service": {"type": "string", "description": "Service name inside the project"},
}


def _service(ctx: ToolContext, args: dict) -> Service:
    service = ctx.db.scalar(
        select(Service).join(Project).where(Project.name == args.get("project"), Service.name == args.get("service"))
    )
    if service is None:
        raise ToolError(f"Service {args.get('project')}/{args.get('service')} not found; call overview to list services")
    return service


def _container_of(ctx: ToolContext, service: Service) -> str | None:
    job = ctx.db.get(Job, service.active_job_id) if service.active_job_id else None
    return job.container if job else None


# ---------- read ----------

def overview(ctx: ToolContext, args: dict) -> str:
    lines = []
    stats = ctx.sampler.read() if ctx.sampler else {}
    if stats:
        cpu, mem, disk = stats.get("cpu") or {}, stats.get("memory") or {}, stats.get("disk") or {}
        lines.append(
            f"Server: CPU {cpu.get('percent', '?')}% of {cpu.get('cores', '?')} cores, load {cpu.get('load', '?')}; "
            f"RAM {mem.get('percent', '?')}% of {round((mem.get('total') or 0) / 2**30, 1)} GB; "
            f"disk {disk.get('percent', '?')}% of {round((disk.get('total') or 0) / 2**30, 1)} GB"
        )
    for project in ctx.db.scalars(select(Project).order_by(Project.name)):
        lines.append(f"\nProject {project.name}:")
        if not project.services and not project.databases:
            lines.append("  (no services or databases)")
        for s in project.services:
            container = _container_of(ctx, s)
            state = "not deployed"
            if container and ctx.docker is not None:
                state = ctx.docker.container_state(container) or "missing"
            last = ctx.db.scalar(select(Job).where(Job.service_id == s.id, Job.kind != JobKind.AGENT)
                                 .order_by(Job.id.desc()).limit(1))
            last_text = f"last deploy #{last.id} {last.status}" if last else "never deployed"
            source = s.image or s.github_repo or s.repo_url or s.archive_name or s.source
            lines.append(f"  service {s.name}: {state}; {last_text}; domain {s.domain or '-'}; source {source}"
                         + (f"; container {container}" if container else ""))
        for d in project.databases:
            state = ctx.docker.container_state(d.container) if ctx.docker is not None else None
            lines.append(f"  database {d.name}: {d.engine} {d.version}, {state or 'missing'}, "
                         f"container {d.container}, host {d.host}")
    if len(lines) <= 1:
        lines.append("No projects yet.")
    return "\n".join(lines)


def service_details(ctx: ToolContext, args: dict) -> str:
    s = _service(ctx, args)
    env = pipelines.load_env(s, ctx.box)
    info = {
        "project": s.project.name, "service": s.name, "source": s.source,
        "repo": s.github_repo or s.repo_url or None, "branch": s.branch, "root_dir": s.root_dir,
        "image": s.image, "template": s.template, "archive": s.archive_name,
        "domain": s.domain, "port": s.port, "start_command": s.start_command,
        "volumes": s.volumes, "internal_host": s.internal_host,
        "databases": [f"{d.name} ({d.engine}, host {d.host})" for d in s.databases],
        # Chỉ tên biến: giá trị có thể là secret
        "env_var_names": sorted(env),
        "active_container": _container_of(ctx, s),
        "recent_jobs": [
            {"id": j.id, "kind": j.kind, "status": j.status, "commit": (j.commit_sha or "")[:7],
             "error": (j.error or "")[:300] or None, "created_at": j.created_at.isoformat()}
            for j in ctx.db.scalars(select(Job).where(Job.service_id == s.id).order_by(Job.id.desc()).limit(8))
        ],
    }
    return json.dumps(info, ensure_ascii=False, indent=1)


def container_logs(ctx: ToolContext, args: dict) -> str:
    tail = max(10, min(int(args.get("tail") or 200), 2000))
    try:
        return _tail(ctx.need_docker().logs(args["container"], tail)) or "(no output)"
    except DockerError as e:
        raise ToolError(str(e)) from None


def job_log(ctx: ToolContext, args: dict) -> str:
    job = ctx.db.get(Job, int(args["job_id"]))
    if job is None:
        raise ToolError("Job not found")
    path = ctx.settings.job_logs_dir / f"{job.id}.log"
    text = path.read_text(errors="replace") if path.is_file() else ""
    head = f"Job #{job.id} {job.kind} {job.status}" + (f", error: {job.error}" if job.error else "")
    return head + "\n" + _tail(text)


def list_containers(ctx: ToolContext, args: dict) -> str:
    try:
        return ctx.need_docker().list_all()
    except DockerError as e:
        raise ToolError(str(e)) from None


def inspect_container(ctx: ToolContext, args: dict) -> str:
    try:
        data = ctx.need_docker().inspect(args["container"])
    except DockerError as e:
        raise ToolError(str(e)) from None
    config = data.get("Config") or {}
    host = data.get("HostConfig") or {}
    info = {
        "name": data.get("Name"), "image": config.get("Image"), "state": data.get("State"),
        "restart_count": data.get("RestartCount"), "cmd": config.get("Cmd"), "entrypoint": config.get("Entrypoint"),
        "working_dir": config.get("WorkingDir"), "user": config.get("User"),
        # Giá trị biến môi trường có thể là secret: chỉ đưa tên
        "env_names": sorted(e.split("=", 1)[0] for e in config.get("Env") or []),
        "labels": config.get("Labels"), "exposed_ports": list((config.get("ExposedPorts") or {}).keys()),
        "mounts": [{"source": m.get("Source") or m.get("Name"), "destination": m.get("Destination"), "rw": m.get("RW")}
                   for m in data.get("Mounts") or []],
        "networks": list(((data.get("NetworkSettings") or {}).get("Networks") or {}).keys()),
        "memory_limit": host.get("Memory"), "restart_policy": host.get("RestartPolicy"),
    }
    return json.dumps(info, ensure_ascii=False, indent=1, default=str)


def http_check(ctx: ToolContext, args: dict) -> str:
    url = str(args.get("url", ""))
    if not url.startswith(("http://", "https://")):
        raise ToolError("url must start with http:// or https://")
    req = urllib.request.Request(url, headers={"User-Agent": "OpsVibes-Agent"})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            status, headers, body = res.status, dict(res.headers), res.read(4000)
    except urllib.error.HTTPError as e:
        status, headers, body = e.code, dict(e.headers), e.read(4000)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return f"Request failed: {getattr(e, 'reason', e)}"
    elapsed = round((time.monotonic() - started) * 1000)
    keep = ("content-type", "location", "server", "x-powered-by", "cache-control", "content-length")
    shown = {k: v for k, v in headers.items() if k.lower() in keep}
    return f"HTTP {status} in {elapsed} ms\nHeaders: {json.dumps(shown)}\n\n{body.decode(errors='replace')}"


def _checkout(ctx: ToolContext, service: Service) -> Path:
    """Bản sao mã nguồn riêng của phiên agent (không đụng workspace mà deploy đang dùng)."""
    if ctx.heal_workspace is not None and service.id == ctx.heal_service_id:
        return ctx.heal_workspace
    dest = ctx.settings.data_dir / "assistant" / str(ctx.session_id) / service.slug
    if (dest / ".git").is_dir():
        return dest
    if service.image:
        raise ToolError("This service runs a prebuilt Docker image; it has no source code to read or edit")
    src = ctx.settings.workspaces_dir / service.slug
    if not (src / ".git").is_dir():
        raise ToolError("No source checkout on the server yet: deploy the service once first")
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "clone", "--quiet", "--no-hardlinks", str(src), str(dest)],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise ToolError(f"Could not copy the source: {result.stderr.strip()[:300]}")
    return dest


def _resolve(root: Path, path: str) -> Path:
    target = (root / path.lstrip("/")).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ToolError("Path must stay inside the service source")
    if ".git" in target.relative_to(root.resolve()).parts:
        raise ToolError("The .git directory is off limits")
    return target


def list_files(ctx: ToolContext, args: dict) -> str:
    service = _service(ctx, args)
    root = _checkout(ctx, service)
    base = _resolve(root, args.get("path") or ".")
    if not base.is_dir():
        raise ToolError("Not a directory")
    entries = []
    for p in sorted(base.iterdir()):
        if p.name == ".git":
            continue
        entries.append(p.name + ("/" if p.is_dir() else f"  ({p.stat().st_size} B)"))
        if len(entries) >= 500:
            entries.append("...")
            break
    return "\n".join(entries) or "(empty)"


def read_file(ctx: ToolContext, args: dict) -> str:
    service = _service(ctx, args)
    target = _resolve(_checkout(ctx, service), args["path"])
    if not target.is_file():
        raise ToolError("File not found")
    data = target.read_bytes()[:MAX_FILE]
    if b"\0" in data[:4000]:
        raise ToolError("Binary file")
    return data.decode(errors="replace")


def _write(ctx: ToolContext, args: dict, config_only: bool) -> str:
    service = _service(ctx, args)
    root = _checkout(ctx, service)
    target = _resolve(root, args["path"])
    rel = target.relative_to(root.resolve()).as_posix()
    if config_only and not is_config_file(rel):
        raise ToolError(f"{rel} is not a build/run config file; use write_source_file (needs user approval)")
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if ctx.heal_workspace is not None and service.id == ctx.heal_service_id:
        return f"Wrote {rel} ({len(content)} chars). The panel rebuilds when you finish."
    return f"Wrote {rel} ({len(content)} chars) in the agent copy. Call deploy_service with apply_changes=true to build it."


def write_config_file(ctx: ToolContext, args: dict) -> str:
    return _write(ctx, args, config_only=True)


def write_source_file(ctx: ToolContext, args: dict) -> str:
    return _write(ctx, args, config_only=False)


def wait_job(ctx: ToolContext, args: dict) -> str:
    job_id = int(args["job_id"])
    deadline = time.monotonic() + max(5, min(int(args.get("timeout") or 600), MAX_WAIT))
    while True:
        ctx.db.expire_all()
        job = ctx.db.get(Job, job_id)
        if job is None:
            raise ToolError("Job not found")
        if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING) or time.monotonic() > deadline:
            break
        if ctx.stopped():
            return f"Stopped waiting: the user pressed Stop. Job #{job.id} is still {job.status} and keeps running."
        time.sleep(2)
    path = ctx.settings.job_logs_dir / f"{job.id}.log"
    text = path.read_text(errors="replace") if path.is_file() else ""
    return f"Job #{job.id} status: {job.status}" + (f", error: {job.error}" if job.error else "") \
        + "\nLog tail:\n" + _tail(text, 6000)


# ---------- auto ----------

def set_env(ctx: ToolContext, args: dict) -> str:
    service = _service(ctx, args)
    env = pipelines.load_env(service, ctx.box)
    updates = args.get("set") or {}
    if not isinstance(updates, dict) or not all(isinstance(v, str) for v in updates.values()):
        raise ToolError("set must map variable names to string values")
    for name in updates:
        if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
            raise ToolError(f"Invalid variable name: {name}")
    env.update(updates)
    for name in args.get("unset") or []:
        env.pop(name, None)
    service.env_enc = ctx.box.encrypt(json.dumps(env)) if env else None
    ctx.db.commit()
    return (f"Saved. Variables now: {', '.join(sorted(env)) or '(none)'}. These are baked in at deploy time, so "
            "they do NOT affect the running container yet — call deploy_service to apply them (it recreates the container).")


def restart(ctx: ToolContext, args: dict) -> str:
    docker = ctx.need_docker()
    if args.get("container"):
        name = args["container"]
    else:
        name = _container_of(ctx, _service(ctx, args))
        if not name:
            raise ToolError("Service has no running container; deploy it instead")
    try:
        docker.restart(name)
    except DockerError as e:
        raise ToolError(str(e)) from None
    return f"Restarted {name}; state now {docker.container_state(name)}"


def deploy(ctx: ToolContext, args: dict) -> str:
    if ctx.queue is None:
        raise ToolError("Not available here: the panel rebuilds automatically when you finish")
    service = _service(ctx, args)
    if service.source == "archive" and not ctx.settings.archive_path(service.id).is_file():
        raise ToolError("The archive file is missing; the user must upload it again")
    patch = None
    if args.get("apply_changes"):
        root = ctx.settings.data_dir / "assistant" / str(ctx.session_id) / service.slug
        if not (root / ".git").is_dir():
            raise ToolError("No changes made yet in this conversation")
        # Job agent mang patch: luồng deploy hiện có áp patch này lên mã nguồn vừa clone
        head = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or None
        patch = Job(service_id=service.id, kind=JobKind.AGENT, status=JobStatus.SUCCESS, trigger=JobTrigger.MANUAL,
                    branch=service.branch, commit_sha=head, agent="assistant",
                    prompt=f"AI Agent #{ctx.session_id}", started_at=utcnow(), finished_at=utcnow())
        ctx.db.add(patch)
        ctx.db.commit()
        ctx.queue.remove_files(patch.id)
        patch.diff = collect_diff(root, ctx.settings.patch_path(patch.id)) or None
        ctx.db.commit()
        if not patch.diff:
            ctx.db.delete(patch)
            ctx.db.commit()
            raise ToolError("No file changes to apply")
    job = ctx.queue.enqueue(ctx.db, service, JobKind.DEPLOY, JobTrigger.MANUAL, patch=patch)
    note = " with the agent's file changes" if patch else ""
    return f"Deploy job #{job.id} queued{note}. Call wait_job to follow it."


# ---------- approve ----------

def _timeout(args: dict) -> int:
    return max(5, min(int(args.get("timeout") or EXEC_TIMEOUT), MAX_EXEC_TIMEOUT))


def exec_in_container(ctx: ToolContext, args: dict) -> str:
    code, out = ctx.need_docker().exec_capture(args["container"], args["command"], _timeout(args))
    return f"exit code {code}\n{_tail(out)}"


def _panel_image(ctx: ToolContext) -> str:
    """Image của chính panel: có sẵn trên máy, có nsenter, không phải kéo thêm."""
    try:
        image = (ctx.need_docker().inspect(ctx.settings.panel_container).get("Config") or {}).get("Image")
    except DockerError:
        image = None
    if not image:
        raise ToolError("Cannot find the panel image to run host commands")
    return image


def host_command(ctx: ToolContext, args: dict) -> str:
    code, out = ctx.need_docker().host_capture(_panel_image(ctx), args["command"], _timeout(args))
    return f"exit code {code}\n{_tail(out)}"


# ---------- tạo mới từ khung chat ----------

def _size(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def _project(ctx: ToolContext, name: str | None) -> Project:
    project = ctx.db.scalar(select(Project).where(Project.name == name))
    if project is None:
        raise ToolError(f"Project {name} not found; create it with create_project")
    return project


def _http(e) -> ToolError:
    return ToolError(str(getattr(e, "detail", e)))


def _attachment(ctx: ToolContext, name: str | None) -> Path:
    try:
        return attachments.path(ctx.settings, ctx.session_id, name or "")
    except FileNotFoundError:
        names = ", ".join(p.name for p in attachments.listing(ctx.settings, ctx.session_id)) or "(none)"
        raise ToolError(f"No attachment named {name!r}. Attached files: {names}") from None


def list_attachments(ctx: ToolContext, args: dict) -> str:
    from .. import databases as dbs

    files = attachments.listing(ctx.settings, ctx.session_id)
    if not files:
        return "The user has not attached any file in this conversation. Ask them to attach it with the paperclip button."
    out = []
    for f in files:
        line = f"- {f.name} ({_size(f.stat().st_size)})"
        if attachments.is_archive(f):
            names = attachments.members(f)
            roots = sorted({n.split("/", 1)[0] for n in names})
            dumps = [n for n in names if n.lower().endswith((".sql", ".sql.gz", ".dump"))]
            hints = [h for h, key in (("WordPress", "wp-settings.php"), ("PHP", "index.php"), ("Node", "package.json"),
                                      ("PHP Composer", "composer.json"), ("Python", "requirements.txt"),
                                      ("Dockerfile", "Dockerfile")) if any(n.rsplit("/", 1)[-1] == key for n in names)]
            line += (f": archive with {len(names)} files; top level: {', '.join(roots[:30])}"
                     + (f"; looks like: {', '.join(hints)}" if hints else "")
                     + (f"; database dumps inside: {', '.join(dumps[:10])}" if dumps else ""))
        else:
            gz, fmt = dbs.detect_format(f, f.name)
            line += f": database dump, format {fmt}{' (gzip)' if gz else ''}"
        out.append(line)
    return "\n".join(out)


def create_project(ctx: ToolContext, args: dict) -> str:
    from pydantic import TypeAdapter, ValidationError

    from ..validators import Name

    try:
        name = TypeAdapter(Name).validate_python(args.get("name"))
    except ValidationError as e:
        raise ToolError(f"Invalid project name: {e.errors()[0]['msg']}") from None
    if ctx.db.scalar(select(Project).where(Project.name == name)):
        return f"Project {name} already exists; use it."
    ctx.db.add(Project(name=name))
    ctx.db.commit()
    return f"Created project {name}."


def create_database(ctx: ToolContext, args: dict) -> str:
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from .. import databases as dbs
    from ..api import databases as db_api

    docker = ctx.need_docker()
    if ctx.sessions is None:
        raise ToolError("Not available here")
    project = _project(ctx, args.get("project"))
    engine = args.get("engine")
    if engine not in dbs.ENGINES:
        raise ToolError(f"engine must be one of {', '.join(dbs.ENGINES)}")
    try:
        database = db_api.new_database(ctx.db, project, engine, args.get("version") or None, args["name"], ctx.box)
        ctx.db.commit()
    except HTTPException as e:
        ctx.db.rollback()
        raise _http(e) from None
    except IntegrityError:
        ctx.db.rollback()
        raise ToolError(f"A database named {args['name']} already exists in {project.name}") from None
    db_api.launch(ctx.sessions, ctx.box, docker, ctx.settings, database)
    return (f"Database {project.name}/{database.name} ({dbs.ENGINES[engine].label} {database.version}) is starting, "
            f"host {database.host}. Pass databases=[\"{database.name}\"] to create_service: the panel adds the "
            "connection env vars (DATABASE_URL, DB_HOST, DB_NAME...) and waits for it to be ready.")


def create_service(ctx: ToolContext, args: dict) -> str:
    from fastapi import HTTPException
    from pydantic import ValidationError
    from sqlalchemy.exc import IntegrityError

    from .. import archive, panel_settings, site_files
    from ..api import databases as db_api
    from ..api.projects import ServiceCreate, new_service

    project = _project(ctx, args.get("project"))
    source = args.get("source")
    fields = {k: args[k] for k in ("name", "port", "domain", "root_dir", "env", "volumes", "start_command")
              if args.get(k) not in (None, "", [], {})}
    upload = None
    if source == "attachment":
        upload = _attachment(ctx, args.get("attachment"))
        if archive.detect(upload) is None:
            raise ToolError(f"{upload.name} is not a .zip/.tar archive; database dumps go to import_database")
        fields["archive"] = True
    elif source == "git":
        fields["repo_url"], fields["branch"] = args.get("repo_url"), args.get("branch") or None
    elif source == "image":
        fields["image"] = args.get("image")
    else:
        raise ToolError("source must be attachment, git or image")
    try:
        req = ServiceCreate(**fields)
    except ValidationError as e:
        raise ToolError("Invalid arguments: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}"
                                                          for x in e.errors())) from None
    linked = []
    try:
        service = new_service(ctx.db, ctx.box, project, req, repo_url="" if source != "git" else req.repo_url,
                              branch=req.branch)
        for name in args.get("databases") or []:
            database = next((d for d in project.databases if d.name == name), None)
            if database is None:
                raise ToolError(f"Database {name} not found in {project.name}; create it with create_database first")
            db_api.link(service, database, ctx.box)
            linked.append(name)
        ctx.db.commit()
    except HTTPException as e:
        ctx.db.rollback()
        raise _http(e) from None
    except IntegrityError:
        ctx.db.rollback()
        raise ToolError(f"Service name {req.name} or domain {req.domain} is already used") from None
    except ToolError:
        ctx.db.rollback()
        raise
    if upload is not None:
        target = ctx.settings.archive_path(service.id)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(upload, target)
        site_files.fresh_marker(ctx.settings, service).touch()
        service.archive_name = upload.name
        ctx.db.commit()
    url = service.domain or panel_settings.default_service_domain(
        panel_settings.get(ctx.db, panel_settings.BASE_DOMAIN), project.name, service.name)
    text = (f"Created service {project.name}/{service.name} (source {service.source}"
            + (f", linked databases: {', '.join(linked)}" if linked else "") + ")"
            + (f", address https://{url}" if url else "") + ".")
    if args.get("deploy", True):
        if ctx.queue is None:
            raise ToolError("Not available here")
        job = ctx.queue.enqueue(ctx.db, service, JobKind.DEPLOY, JobTrigger.MANUAL)
        text += f" Deploy job #{job.id} queued; call wait_job to follow it."
    return text


def _import_target(ctx: ToolContext, args: dict) -> Database:
    database = ctx.db.scalar(select(Database).join(Project).where(Project.name == args.get("project"),
                                                                  Database.name == args.get("database")))
    if database is None:
        raise ToolError(f"Database {args.get('project')}/{args.get('database')} not found")
    return database


def _database_is_empty(ctx: ToolContext, args: dict) -> bool:
    """Database chưa từng import và chưa service nào chạy với nó: nạp vào không mất gì, khỏi cần duyệt."""
    database = _import_target(ctx, args)
    users = [s for s in database.project.services if database in s.databases]
    return database.import_status is None and not any(s.active_job_id for s in users)


def import_database(ctx: ToolContext, args: dict) -> str:
    from ..api import databases as db_api

    docker = ctx.need_docker()
    if ctx.sessions is None:
        raise ToolError("Not available here")
    database = _import_target(ctx, args)
    if database.import_status == "running":
        raise ToolError("An import is already running on this database; wait for it")
    upload = _attachment(ctx, args.get("attachment"))
    member = args.get("path") or None
    if attachments.is_archive(upload) and not member:
        dumps = [n for n in attachments.members(upload) if n.lower().endswith((".sql", ".sql.gz", ".dump"))]
        if len(dumps) != 1:
            raise ToolError("This attachment is an archive: pass path= the dump file inside it"
                            + (f" (found: {', '.join(dumps[:10])})" if dumps else " (no .sql file found inside)"))
        member = dumps[0]
    clean = not _database_is_empty(ctx, args)
    dump = ctx.settings.data_dir / "imports" / f"{database.id}-{time.time_ns()}-agent"
    dump.parent.mkdir(parents=True, exist_ok=True)
    try:
        if member:
            attachments.extract_member(upload, member, dump)
        else:
            shutil.copyfile(upload, dump)
    except KeyError:
        dump.unlink(missing_ok=True)
        raise ToolError(f"{member} is not in {upload.name}") from None
    database.import_status, database.error = "running", None
    ctx.db.commit()
    filename = Path(member).name if member else upload.name
    # Chạy ngay trong lượt này (có thể vài phút với dump lớn): agent biết kết quả trước khi deploy
    db_api._run_import(ctx.sessions, ctx.box, docker, ctx.settings, database.id, dump, filename, clean=clean)
    ctx.db.refresh(database)
    if database.import_status != "success":
        raise ToolError(f"Import failed: {database.error}")
    return (f"Imported {filename} into {database.project.name}/{database.name}. A WordPress service deployed "
            "afterwards will not import the dump inside its archive again.")


_REASON = {"type": "string", "description": "One sentence for the user: why this command is needed"}
_TIMEOUT = {"type": "integer", "description": f"Seconds, default {EXEC_TIMEOUT}, max {MAX_EXEC_TIMEOUT}"}

TOOLS: dict[str, ToolDef] = {t.name: t for t in (
    ToolDef("overview", "Server resources, every project with its services (container state, last deploy, domain) "
            "and databases. Start here.", _obj({}), READ, overview),
    ToolDef("service_details", "Full config of one service: source, branch, domain, port, start command, volumes, "
            "linked databases, env var names (values hidden), active container and recent jobs with errors.",
            _obj(_SERVICE_ARGS, ["project", "service"]), READ, service_details),
    ToolDef("container_logs", "Recent stdout/stderr of any container on the server (app, database, "
            "opsvibes-traefik for routing/SSL issues, opsvibes for the panel).",
            _obj({"container": _STR, "tail": {"type": "integer", "description": "Lines, default 200, max 2000"}},
                 ["container"]), READ, container_logs),
    ToolDef("job_log", "Build/deploy log of a job (ids come from service_details).",
            _obj({"job_id": {"type": "integer"}}, ["job_id"]), READ, job_log),
    ToolDef("wait_job", "Wait until a queued/running job finishes, then return its status and log tail.",
            _obj({"job_id": {"type": "integer"}, "timeout": {"type": "integer", "description": "Seconds, max 900"}},
                 ["job_id"]), READ, wait_job),
    ToolDef("list_containers", "All containers on the server, including ones not managed by the panel.",
            _obj({}), READ, list_containers),
    ToolDef("inspect_container", "Image, state, restart count, command, mounts, networks and env var names of a "
            "container.", _obj({"container": _STR}, ["container"]), READ, inspect_container),
    ToolDef("http_check", "Send a GET request from the panel (inside the server's Docker network) and return "
            "status, key headers and the start of the body. Containers are reachable by name.",
            _obj({"url": _STR}, ["url"]), READ, http_check),
    ToolDef("list_files", "List a directory of a service's source code (the agent's own copy).",
            _obj({**_SERVICE_ARGS, "path": {"type": "string", "description": "Relative path, default root"}},
                 ["project", "service"]), READ, list_files),
    ToolDef("read_file", "Read a text file from a service's source code.",
            _obj({**_SERVICE_ARGS, "path": _STR}, ["project", "service", "path"]), READ, read_file),
    ToolDef("write_config_file", "Create or overwrite a build/run config file (Dockerfile, nixpacks.toml, package.json, "
            "requirements.txt, framework config, wp-config.php, .htaccess...) in the agent's copy of the source. "
            "Changes go live only through deploy_service with apply_changes=true.",
            _obj({**_SERVICE_ARGS, "path": _STR, "content": {"type": "string", "description": "Full new file content"}},
                 ["project", "service", "path", "content"]), AUTO, write_config_file),
    ToolDef("set_env", "Set or remove environment variables of a service (saved by the panel, applied on next "
            "deploy).", _obj({**_SERVICE_ARGS, "set": {"type": "object", "description": "NAME -> value"},
                              "unset": {"type": "array", "items": _STR}}, ["project", "service"]), AUTO, set_env),
    ToolDef("restart", "Restart a service's active container, or any container by name.",
            _obj({**_SERVICE_ARGS, "container": {"type": "string", "description": "Container name instead of a service"}}),
            AUTO, restart),
    ToolDef("deploy_service", "Queue a new build and deploy of a service. apply_changes=true builds the files you "
            "changed in this conversation on top of the latest source.",
            _obj({**_SERVICE_ARGS, "apply_changes": {"type": "boolean"}}, ["project", "service"]), AUTO, deploy),
    ToolDef("list_attachments", "Files the user attached in this chat (archives with their top-level content and "
            "detected stack, database dumps with their format).", _obj({}), READ, list_attachments),
    ToolDef("create_project", "Create a new project (a group of services and databases).",
            _obj({"name": {"type": "string", "description": "lowercase letters, digits, '-'"}}, ["name"]), AUTO,
            create_project),
    ToolDef("create_database", "Create a database in a project and start it (engines: postgres, mysql, mariadb, "
            "mongo, redis).",
            _obj({"project": _STR, "name": _STR, "engine": {"type": "string", "enum": ["postgres", "mysql", "mariadb",
                                                                                       "mongo", "redis"]},
                  "version": {"type": "string", "description": "Optional; default is the latest supported"}},
                 ["project", "name", "engine"]), AUTO, create_database),
    ToolDef("create_service", "Create a service in a project and deploy it. source=attachment uses an archive the user "
            "attached (WordPress / PHP / Node...: the panel detects the stack, configures WordPress and keeps "
            "uploads); source=git a public git URL; source=image a Docker image. databases links existing databases "
            "of the project (connection env vars are added).",
            _obj({"project": _STR, "name": _STR,
                  "source": {"type": "string", "enum": ["attachment", "git", "image"]},
                  "attachment": {"type": "string", "description": "Attachment file name (source=attachment)"},
                  "repo_url": _STR, "branch": _STR, "image": _STR,
                  "databases": {"type": "array", "items": _STR},
                  "port": {"type": "integer", "description": "Only if the app does not listen on $PORT"},
                  "domain": {"type": "string", "description": "Custom domain; omit to use the default address"},
                  "root_dir": {"type": "string", "description": "Subfolder holding the app, if not the root"},
                  "env": {"type": "object", "description": "NAME -> value"},
                  "volumes": {"type": "array", "items": _STR, "description": "Folders to persist, e.g. /app/data"},
                  "start_command": _STR,
                  "deploy": {"type": "boolean", "description": "Default true"}},
                 ["project", "name", "source"]), AUTO, create_service),
    ToolDef("import_database", "Load an attached dump (.sql, .sql.gz, .dump, mongo archive/json) into a database, "
            "or a dump inside an attached archive (path=). Runs right away into an empty database; overwriting a "
            "database that already has data needs user approval.",
            _obj({"project": _STR, "database": _STR, "attachment": _STR,
                  "path": {"type": "string", "description": "Dump file inside the archive, if the attachment is one"},
                  "reason": _REASON},
                 ["project", "database", "attachment", "reason"]), APPROVE, import_database, auto_if=_database_is_empty),
    ToolDef("write_source_file", "Create or overwrite an application source file (not a config file). Needs user "
            "approval; prefer config fixes.",
            _obj({**_SERVICE_ARGS, "path": _STR, "content": _STR, "reason": _REASON},
                 ["project", "service", "path", "content", "reason"]), APPROVE, write_source_file),
    ToolDef("exec_in_container", "Run a shell command inside a container (sh -c). Needs user approval.",
            _obj({"container": _STR, "command": _STR, "reason": _REASON, "timeout": _TIMEOUT},
                 ["container", "command", "reason"]), APPROVE, exec_in_container),
    ToolDef("host_command", "Run a shell command as root on the server itself (outside Docker). Needs user approval. "
            "Use for things the other tools cannot see: systemd, firewall, disk, DNS, kernel logs.",
            _obj({"command": _STR, "reason": _REASON, "timeout": _TIMEOUT}, ["command", "reason"]), APPROVE,
            host_command),
)}


# Chỉ dùng trong khung chat (cần file đính kèm / tạo mới): không đưa cho tự sửa lúc deploy hay CLI qua MCP
CHAT_ONLY = {"list_attachments", "create_project", "create_database", "create_service", "import_database"}


def needs_approval(ctx: ToolContext, tool: ToolDef, args: dict) -> bool:
    if tool.risk != APPROVE:
        return False
    if tool.auto_if is None:
        return True
    try:
        return not tool.auto_if(ctx, args)
    except Exception:  # noqa: BLE001 - không xác định được thì hỏi người dùng
        return True


def run(ctx: ToolContext, name: str, args: dict) -> tuple[str, bool]:
    """Chạy tool, trả (kết quả, có_lỗi). Không kiểm tra quyền: session.py làm việc đó trước khi gọi."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"Unknown tool {name}", True
    try:
        return tool.handler(ctx, args), False
    except ToolError as e:
        return str(e), True
    except (KeyError, TypeError, ValueError) as e:
        return f"Invalid arguments: {e}", True
    except DockerError as e:
        return str(e), True
