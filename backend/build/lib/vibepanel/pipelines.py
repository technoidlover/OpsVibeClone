"""Các pipeline mà JobQueue thực thi.

deploy:   lấy mã nguồn -> build image (Dockerfile nếu có, không thì Nixpacks) -> release
          Service dạng image (ứng dụng 1-click): kéo image -> release
rollback: release lại image của một job cũ
release:  chạy container mới, chờ nhận kết nối, chuyển route Traefik, rồi mới gỡ bản cũ.
          Bản mới không lên thì bản cũ vẫn phục vụ bình thường.

AI chỉ là bước tùy chọn sẽ thêm ở bước 3, khi người dùng đã kết nối AI provider.
"""

import json
import re
import shutil
import subprocess
import time

from sqlalchemy import select

from . import archive, custom_files, detect, github, panel_settings, refs, runner, site_files, templates, wordpress
from .i18n import t
from .crypto import SecretBox
from .docker import JOB_LABEL, SERVICE_LABEL, DockerError
from .jobs import JobContext, Pipeline
from .models import Job, JobKind, JobStatus, Service
from .proxy import default_domain

DEFAULT_PORT = 3000
HEALTH_TIMEOUT = 90
# Worker (không mở cổng) phải chạy liên tục chừng này giây mới coi là lên được
WORKER_SETTLE = 15
# Sau khi cổng mở, chờ đường dẫn health check trả 2xx/3xx tối đa chừng này giây
HTTP_HEALTH_TIMEOUT = 60
# Ứng dụng 1-click lần đầu chạy migration database (Ghost, n8n...) nên chờ lâu hơn
IMAGE_HEALTH_TIMEOUT = 300


def load_env(service: Service, box: SecretBox) -> dict[str, str]:
    raw = box.decrypt_optional(service.env_enc)
    return json.loads(raw) if raw else {}


def git_token(ctx: JobContext) -> str | None:
    """Repo kết nối qua GitHub App: xin token cài đặt mới; không thì dùng token đã lưu."""
    service = ctx.service
    if service.github_installation_id is None:
        return ctx.box.decrypt_optional(service.git_token_enc)
    app = github.get_app(ctx.db)
    if app is None:
        raise RuntimeError(t("deploy.github_disconnected"))
    try:
        return github.installation_token(app, ctx.box, service.github_installation_id)
    except github.GitHubError as e:
        raise RuntimeError(t("deploy.github_token_failed", error=e)) from e


def prepare_source(ctx: JobContext) -> None:
    """Đưa mã nguồn mới nhất vào workspace: clone repo, giải nén file đã tải lên hoặc gom file riêng."""
    if ctx.service.source == "manual":
        assemble_manual(ctx)
    elif ctx.service.source == "archive":
        extract_source(ctx)
    else:
        fetch_source(ctx)


_GIT = ["git", "-c", "user.name=OpsVibes", "-c", "user.email=opsvibes@localhost", "-c", "safe.directory=*"]


def _git_commit(ctx: JobContext, cwd, message: str, init: bool = False) -> str:
    """Commit mọi file trong thư mục (git cục bộ để agent tính diff và áp patch). Trả SHA."""
    steps = [["add", "-A"], ["commit", "-q", "--no-verify", "--allow-empty", "-m", message]]
    for args in ([["init", "-q", "-b", "main"]] if init else []) + steps:
        # Lệnh nội bộ: không ghi vào log deploy trừ khi lỗi
        done = subprocess.run([*_GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            ctx.log.write(done.stderr.strip())
            raise RuntimeError(t("archive.git_failed"))
    return subprocess.run([*_GIT, "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()


def assemble_manual(ctx: JobContext) -> None:
    """Service tự viết: mã nguồn chỉ gồm file riêng (Dockerfile, compose.yml, .sql...)."""
    files = ctx.settings.files_dir(ctx.service.id)
    if not custom_files.list_files(files):
        raise RuntimeError(t("files.manual_empty"))
    ctx.log.write(t("files.assemble"))
    workspace = ctx.workspace
    staging = workspace.with_name(workspace.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for path in custom_files.apply(files, staging):
        ctx.log.write(f"    {path}")
    sha = _git_commit(ctx, staging, "custom files", init=True)
    shutil.rmtree(workspace, ignore_errors=True)
    staging.rename(workspace)
    ctx.job.commit_sha = sha
    ctx.job.commit_message = None
    ctx.db.commit()


def apply_custom_files(ctx: JobContext) -> None:
    """Chép file riêng đè lên thư mục build của repo / file nén, rồi commit cục bộ để diff của AI sạch."""
    if ctx.service.source == "manual":
        return
    copied = custom_files.apply(ctx.settings.files_dir(ctx.service.id), ctx.build_dir)
    if not copied:
        return
    ctx.log.write(t("files.applied", files=", ".join(copied)))
    _git_commit(ctx, ctx.workspace, "OpsVibes custom files")


def extract_source(ctx: JobContext) -> None:
    service = ctx.service
    path = ctx.settings.archive_path(service.id)
    if not path.is_file():
        raise RuntimeError(t("archive.missing"))
    ctx.log.write(t("archive.extracting", name=service.archive_name or path.name))
    workspace = ctx.workspace
    staging = workspace.with_name(workspace.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        flattened = archive.extract(path, staging)
    except archive.ArchiveError as e:
        shutil.rmtree(staging, ignore_errors=True)
        raise RuntimeError(str(e)) from None
    if flattened:
        ctx.log.write(t("archive.flattened", folder=flattened))
    # Git cục bộ để agent tính diff, hoàn tác file ngoài cấu hình và áp patch như repo thường
    sha = _git_commit(ctx, staging, service.archive_name or "archive", init=True)

    shutil.rmtree(workspace, ignore_errors=True)
    staging.rename(workspace)
    ctx.job.commit_sha = sha
    ctx.job.commit_message = (service.archive_name or "")[:300] or None
    ctx.db.commit()


def _commit_subject(workspace) -> str | None:
    done = subprocess.run(["git", "-c", "safe.directory=*", "log", "-1", "--format=%s"], cwd=workspace,
                          capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        return None
    return done.stdout.strip()[:300] or None


def fetch_source(ctx: JobContext) -> None:
    service = ctx.service
    token = git_token(ctx)
    git_env = runner.git_auth_env(token)
    ctx.log.add_secret(token)
    ctx.log.add_secret(git_env.get("GIT_CONFIG_VALUE_0"))

    ctx.log.write(t("deploy.fetch_source", repo=service.repo_url, branch=ctx.job.branch))
    workspace = ctx.workspace
    staging = workspace.with_name(workspace.name + ".incoming")
    shutil.rmtree(staging, ignore_errors=True)

    # Clone vào thư mục tạm rồi mới thay thế, để clone lỗi không xóa mất bản đang có
    runner.run(
        [
            "git", "clone", "--depth", "1", "--single-branch",
            "--branch", ctx.job.branch,
            "--", service.repo_url, str(staging),
        ],
        ctx.log,
        env=git_env,
        timeout=ctx.settings.build_timeout_seconds,
    )
    sha = runner.run(["git", "rev-parse", "HEAD"], ctx.log, cwd=staging).strip()

    shutil.rmtree(workspace, ignore_errors=True)
    staging.rename(workspace)
    ctx.job.commit_sha = sha
    ctx.job.commit_message = _commit_subject(workspace)
    ctx.db.commit()


# Biến chắc chắn không bí mật: không che trong log để log còn đọc được
_PUBLIC_ENV = re.compile(
    r"^(PORT|NODE_ENV|TZ|LANG|LOG_LEVEL|DEBUG|NIXPACKS_\w+|\w+_(HOST|PORT|NAME|USER|USERNAME|ENV|MODE|REGION))$"
)


def register_env_secrets(log, env: dict[str, str]) -> None:
    for key, value in env.items():
        if not (_PUBLIC_ENV.match(key) or value.isdigit()):
            log.add_secret(value)


def render_env(ctx: JobContext, env: dict[str, str], warn: bool = False) -> dict[str, str]:
    """Thay ${VP_URL}/${VP_DOMAIN} (chính service) và ${tên.URL}... (service khác, xem refs.py)."""
    env = templates.render_env(env, public_url(ctx))
    base_domain = panel_settings.get(ctx.db, panel_settings.BASE_DOMAIN)

    def port_of(service: Service) -> int | None:
        if service.port:
            return service.port
        active = ctx.db.get(Job, service.active_job_id) if service.active_job_id else None
        return active.port if active else None

    env, missing = refs.resolve(env, ctx.service, base_domain, port_of)
    if warn:
        for ref in dict.fromkeys(missing):
            ctx.log.write(t("deploy.ref_missing", ref=ref))
    return env


def check_build_dir(ctx: JobContext) -> None:
    if ctx.service.root_dir:
        if not ctx.build_dir.is_dir():
            raise RuntimeError(t("deploy.root_dir_missing", path=ctx.service.root_dir))
        ctx.log.write(t("deploy.root_dir", path=ctx.service.root_dir))


def build_image(ctx: JobContext) -> str:
    image = f"vp/{ctx.service.slug}:{ctx.job.id}"
    # Biến tham chiếu cần giải ngay lúc build: frontend nhúng VITE_* / NEXT_PUBLIC_* vào mã khi build
    env = render_env(ctx, load_env(ctx.service, ctx.box), warn=True)
    register_env_secrets(ctx.log, env)

    if ctx.service.source == "manual" and not (ctx.build_dir / "Dockerfile").is_file():
        raise RuntimeError(t("files.manual_needs_dockerfile"))
    if (ctx.build_dir / "Dockerfile").is_file():
        ctx.log.write(t("deploy.build_dockerfile", image=image))
        ctx.docker.build_dockerfile(ctx.build_dir, image, ctx.log, ctx.settings.build_timeout_seconds, build_args=env)
    else:
        ctx.log.write(t("deploy.build_nixpacks", image=image))
        tuning = _tune_nixpacks(ctx, env)
        ctx.docker.build_nixpacks(
            ctx.build_dir, image, {**env, **tuning.env}, ctx.log, ctx.settings.build_timeout_seconds,
            start_cmd=tuning.start, install_cmd=tuning.install, build_cmd=tuning.build,
        )

    ctx.job.image = image
    ctx.db.commit()
    return image


def _tune_nixpacks(ctx: JobContext, env: dict[str, str]) -> detect.Tuning:
    """Gom các quy tắc bù cho Nixpacks; ghi log từng điều chỉnh để người dùng biết."""
    tuning = detect.node_version_env(ctx.build_dir, env)
    try:
        plan = ctx.docker.nixpacks_plan(ctx.build_dir, {**env, **tuning.env})
        spa = detect.spa_env(ctx.build_dir, plan, env)
        if spa.env:
            tuning.env.update(spa.env)
            tuning.notes += spa.notes
            plan = ctx.docker.nixpacks_plan(ctx.build_dir, {**env, **tuning.env})
    except (DockerError, ValueError) as e:
        ctx.log.write(t("deploy.nixpacks_plan_unreadable", error=e))
        plan = None

    if plan is not None:
        bun = detect.bun_fallback(ctx.build_dir, plan, detect.cpu_flags(ctx.settings.host_proc))
        tuning.install, tuning.build, tuning.start = bun.install, bun.build, bun.start
        tuning.notes += bun.notes

    if ctx.service.start_command:
        tuning.start = ctx.service.start_command
        tuning.notes.append(t("detect.start_from_settings", command=tuning.start))
    elif plan is not None and (ctx.site_mode in ("reuse", "fresh") or wordpress.is_wordpress(ctx.build_dir)):
        start = tuning.start or (plan.get("start") or {}).get("cmd")
        if start and ctx.site_mode in ("reuse", "fresh"):
            tuning.start = site_files.CHOWN_PREFIX + start
            tuning.notes.append(t("site.chown"))
        elif start:
            tuning.start = wordpress.CHOWN_PREFIX + start
            tuning.notes.append(t("wp.chown"))
    elif plan is not None and not tuning.start and not (plan.get("start") or {}).get("cmd"):
        guess = detect.guess_start_command(ctx.build_dir)
        if guess is None:
            raise RuntimeError(t("deploy.no_start_command"))
        tuning.start, reason = guess
        tuning.notes.append(t("detect.start_guessed", reason=reason, command=tuning.start))

    for note in tuning.notes:
        ctx.log.write(t("deploy.adjustment", note=note))
    return tuning


def pull_image(ctx: JobContext) -> str:
    image = ctx.service.image
    ctx.log.write(t("deploy.pull_image", image=image))
    ctx.docker.pull(image, ctx.log, ctx.settings.build_timeout_seconds)
    ctx.job.image = image
    ctx.job.commit_message = image
    ctx.db.commit()
    return image


def public_url(ctx: JobContext) -> str | None:
    service = ctx.service
    host = service.domain or default_domain(service, panel_settings.get(ctx.db, panel_settings.BASE_DOMAIN))
    return f"https://{host}" if host else None


def _resolve_port(ctx: JobContext, image: str) -> int:
    if ctx.service.port:
        return ctx.service.port
    exposed = ctx.docker.exposed_ports(image)
    if len(exposed) == 1:
        return exposed[0]
    return DEFAULT_PORT


# Thông báo thiếu biến môi trường hay gặp khi app crash lúc khởi động
_MISSING_ENV_PATTERNS = [
    re.compile(r'Configuration key "([A-Z][A-Z0-9_]*)" does not exist'),  # NestJS ConfigService
    re.compile(r"KeyError: '([A-Z][A-Z0-9_]*)'"),  # Python os.environ[...]
    re.compile(r"[Mm]issing (?:required )?(?:environment|env) variables?:?\s+['\"`]?([A-Z][A-Z0-9_]*)"),
    re.compile(r"(?:environment|env) variable ['\"`]?([A-Z][A-Z0-9_]*)['\"`]? (?:is )?(?:not set|missing|required)"),
    re.compile(r"\b([A-Z][A-Z0-9_]*) (?:environment variable )?is (?:not set|required|missing)\b"),
]


def missing_env_vars(log: str) -> list[str]:
    found: list[str] = []
    for pattern in _MISSING_ENV_PATTERNS:
        for name in pattern.findall(log):
            if name not in found:
                found.append(name)
    return found


def _dump_logs(ctx: JobContext, container: str) -> str:
    log = ""
    try:
        ctx.log.write(t("deploy.container_log"))
        log = ctx.docker.logs(container, 50)
        for line in log.splitlines():
            ctx.log.write(line)
    except DockerError:
        pass
    return log


def startup_error(ctx: JobContext, container: str, port: int | None, timeout: int) -> RuntimeError:
    """App mới không nhận kết nối: ghi log container và trả lỗi nói đúng nguyên nhân nhất có thể."""
    crashed = port is None or ctx.docker.container_state(container) != "running"
    log = _dump_logs(ctx, container)
    if missing := missing_env_vars(log):
        return RuntimeError(t("deploy.missing_env", names=", ".join(missing)))
    if crashed:
        return RuntimeError(t("deploy.app_crashed"))
    return RuntimeError(t("deploy.port_timeout", port=port, timeout=timeout))


def wait_healthy(ctx: JobContext, container: str, port: int | None, timeout: int) -> RuntimeError | None:
    """Chờ bản mới lên theo kiểu kiểm tra của service (cổng TCP / đường dẫn HTTP / worker). None = ổn."""
    service, docker = ctx.service, ctx.docker
    if service.health_check == "none":
        ctx.log.write(t("deploy.wait_worker", seconds=WORKER_SETTLE))
        return None if docker.wait_stable(container, WORKER_SETTLE) else startup_error(ctx, container, None, timeout)
    if not docker.wait_for_port(container, port, timeout):
        return startup_error(ctx, container, port, timeout)
    if service.health_check == "http":
        path = service.health_path or "/"
        ctx.log.write(t("deploy.wait_http", path=path, port=port))
        ok, detail = docker.wait_http(container, port, path, HTTP_HEALTH_TIMEOUT)
        if not ok:
            _dump_logs(ctx, container)
            return RuntimeError(t("deploy.http_unhealthy", path=path, detail=detail, timeout=HTTP_HEALTH_TIMEOUT))
    return None


def release(ctx: JobContext, image: str) -> None:
    service, job, docker = ctx.service, ctx.job, ctx.docker
    # Worker không nhận traffic: không có cổng, Traefik không route tới
    port = None if service.health_check == "none" else _resolve_port(ctx, image)
    env = {**({"PORT": str(port)} if port else {}), **render_env(ctx, load_env(service, ctx.box))}
    register_env_secrets(ctx.log, env)
    container = f"vp_{service.slug}_{job.id}"
    volumes = {templates.volume_name(service.slug, m): m for m in service.volumes}
    site_new = None
    if ctx.site_mode == "fresh":
        site_new = site_files.new_name(service, job.id)
        volumes = {site_new: site_files.MOUNT, **volumes}
        ctx.log.write(t("site.fresh", volume=site_new))
    elif service.site_volume and ctx.site_mode in ("reuse", None) and not service.image:
        volumes = {service.site_volume: site_files.MOUNT, **volumes}
        ctx.log.write(t("site.reuse", volume=service.site_volume))
    timeout = IMAGE_HEALTH_TIMEOUT if service.image else HEALTH_TIMEOUT

    # Hai bản cùng ghi một volume (vd SQLite, file upload) có thể hỏng dữ liệu: dừng bản cũ trước
    stopped: list[str] = []
    if volumes:
        stopped = [n for n in docker.containers_for_service(service.id) if n != container]
        for name in stopped:
            ctx.log.write(t("deploy.stop_old", name=name))
            docker.stop(name)

    ctx.log.write(t("deploy.start_container", container=container, port=port))
    for volume, mount in volumes.items():
        ctx.log.write(f"    volume {volume} -> {mount}")
    docker.remove_container(container)
    docker.run_app(
        container, image, env, {SERVICE_LABEL: str(service.id), JOB_LABEL: str(job.id)}, ctx.log,
        volumes=volumes or None, aliases=[service.internal_host],
    )

    if error := wait_healthy(ctx, container, port, timeout):
        docker.remove_container(container)
        if site_new:
            docker.remove_volume(site_new)
        for name in stopped:
            ctx.log.write(t("deploy.restart_old", name=name))
            try:
                docker.start(name)
            except DockerError as e:
                ctx.log.write(t("deploy.restart_old_failed", name=name, error=e))
        raise error

    job.container, job.port = container, port
    service.active_job_id = job.id
    old_site = None
    if site_new:
        old_site, service.site_volume = service.site_volume, site_new
        site_files.fresh_marker(ctx.settings, service).unlink(missing_ok=True)
    ctx.db.commit()

    # Chờ Traefik đọc route mới (poll 5 giây) rồi mới gỡ container cũ
    ctx.log.write(t("deploy.switch_traffic"))
    time.sleep(ctx.settings.route_switch_grace_seconds)
    for name in docker.containers_for_service(service.id):
        if name != container:
            ctx.log.write(t("deploy.remove_old", name=name))
            docker.remove_container(name)
    if old_site and old_site != site_new:
        docker.remove_volume(old_site)

    _prune_images(ctx)


def _prune_images(ctx: JobContext) -> None:
    """Giữ image của vài lần deploy thành công gần nhất để rollback."""
    jobs = ctx.db.scalars(
        select(Job)
        .where(Job.service_id == ctx.service.id, Job.image.is_not(None))
        .order_by(Job.id.desc())
    ).all()
    keep: set[str] = {ctx.job.image} if ctx.job.image else set()
    for job in jobs:
        if job.status == JobStatus.SUCCESS and len(keep) < ctx.settings.keep_images:
            keep.add(job.image)
    for image in {j.image for j in jobs} - keep:
        ctx.docker.remove_image(image)


def apply_patch(ctx: JobContext) -> None:
    """Áp thay đổi của một lần chạy agent lên mã nguồn vừa clone."""
    source = ctx.db.get(Job, ctx.job.patch_job_id)
    path = ctx.settings.patch_path(ctx.job.patch_job_id)
    if source is None or source.service_id != ctx.service.id or not path.is_file():
        raise RuntimeError(t("deploy.patch_not_found", job_id=ctx.job.patch_job_id))
    ctx.log.write(t("deploy.apply_patch", job_id=source.id))
    try:
        runner.run(["git", "-c", "safe.directory=*", "apply", "--whitespace=nowarn", str(path)], ctx.log, cwd=ctx.workspace)
    except runner.CommandError as e:
        raise RuntimeError(t("deploy.apply_patch_failed", job_id=source.id)) from e


class DatabaseNotReady(RuntimeError):
    """Database liên kết chưa sẵn sàng: không phải lỗi cấu hình nên không nhờ AI sửa."""


def wait_for_databases(ctx: JobContext) -> None:
    """App thường kết nối DB (và chạy migration) ngay khi khởi động: chờ DB đã liên kết sẵn sàng."""
    from . import databases as dbs

    for database in ctx.service.databases:
        label = f"{dbs.ENGINES[database.engine].label} {database.name}"
        deadline = time.monotonic() + dbs.IMPORT_TIMEOUT
        announced = False
        while True:
            ctx.db.refresh(database)
            if database.import_status != "running":
                break
            if not announced:
                ctx.log.write(t("db.wait_import", label=label))
                announced = True
            if time.monotonic() > deadline:
                raise DatabaseNotReady(t("db.import_timeout", label=label))
            time.sleep(3)
        if database.import_status == "failed" and announced:
            ctx.log.write(t("db.import_failed_warning", label=label, error=database.error))
        ctx.log.write(t("db.wait_ready", label=label, host=database.host, port=dbs.ENGINES[database.engine].port))
        if not dbs.wait_ready(ctx.docker, database):
            raise DatabaseNotReady(t("db.ready_timeout", label=label, timeout=dbs.READY_TIMEOUT))


def deploy(ctx: JobContext) -> None:
    from . import compose
    from .assistant import heal  # heal -> tools dùng load_env của module này

    if ctx.service.image:
        image = pull_image(ctx)
        wait_for_databases(ctx)
        release(ctx, image)
        return

    prepare_source(ctx)
    check_build_dir(ctx)
    ctx.site_mode = _site_mode(ctx)
    apply_custom_files(ctx)
    if ctx.job.patch_job_id:
        apply_patch(ctx)
    if ctx.job.ai_prepare and ctx.job.heal_agent:
        heal.prepare(ctx, ctx.job.heal_agent)
    dumps = prepare_wordpress(ctx)
    attempt = 0
    while True:
        try:
            if ctx.service.build_mode == "compose":
                wait_for_databases(ctx)
                compose.deploy(ctx)
            else:
                image = build_image(ctx)
                wait_for_databases(ctx)
                if dumps:
                    import_dump(ctx, dumps)
                    dumps = []
                release(ctx, image)
            _keep_ai_changes(ctx)
            return
        except DatabaseNotReady:
            raise
        except Exception as e:  # noqa: BLE001 - lỗi nào cũng có thể do cấu hình
            attempt += 1
            brain = ctx.job.heal_agent
            if not brain or attempt > heal.MAX_HEAL_ATTEMPTS or not heal.heal(ctx, brain, e, attempt):
                raise


def _site_mode(ctx: JobContext) -> str:
    """Có chạy mã nguồn từ volume không, và dùng lại volume đang có hay dựng lại (xem site_files.py)."""
    service = ctx.service
    if not site_files.wanted(service, ctx.build_dir):
        return "off"
    if (service.site_volume and ctx.docker.volume_exists(service.site_volume)
            and not site_files.fresh_marker(ctx.settings, service).exists()):
        return "reuse"
    return "fresh"


def _keep_ai_changes(ctx: JobContext) -> None:
    """Service tự viết không có repo để commit: giữ lại file AI Agent đã sửa cho các lần deploy sau."""
    if ctx.service.source != "manual" or not ctx.job.assistant_session_id:
        return
    changed = custom_files.sync_back(ctx.workspace, ctx.settings.files_dir(ctx.service.id))
    if changed:
        ctx.log.write(t("files.ai_saved", files=", ".join(changed)))


def prepare_wordpress(ctx: JobContext) -> list:
    """WordPress: sửa wp-config, dọn file dump khỏi thư mục web... (xem wordpress.py). Trả các file dump."""
    if not wordpress.is_wordpress(ctx.build_dir):
        return []
    ctx.log.write(t("wp.detected"))
    nixpacks = not (ctx.build_dir / "Dockerfile").is_file()
    stash = ctx.settings.data_dir / "imports" / f"wp-{ctx.job.id}"
    dumps = wordpress.prepare(ctx.build_dir, stash, nixpacks, ctx.log)
    if not dumps:
        shutil.rmtree(stash, ignore_errors=True)
    if nixpacks and not ctx.service.volumes:
        ctx.service.volumes = [wordpress.UPLOADS_MOUNT]
        ctx.db.commit()
        ctx.log.write(t("wp.uploads_volume", mount=wordpress.UPLOADS_MOUNT))
    return dumps


def import_dump(ctx: JobContext, dumps: list) -> None:
    """Import file dump có sẵn trong mã nguồn vào database MySQL/MariaDB liên kết, nếu database đó chưa từng import.

    Lỗi import chỉ ghi cảnh báo: app vẫn được khởi động, người dùng import lại ở trang database.
    """
    from . import databases as dbs

    try:
        targets = [d for d in ctx.service.databases if d.engine in ("mysql", "mariadb")]
        if len(dumps) > 1:
            ctx.log.write(t("wp.dump_many", names=", ".join(p.name for p in dumps)))
            return
        dump = dumps[0]
        if not targets:
            ctx.log.write(t("wp.dump_no_db", name=dump.name))
            return
        database = targets[0]
        if database.import_status is not None:
            ctx.log.write(t("wp.dump_skipped", name=dump.name, db=database.name))
            return
        for value in dbs.container_env(database, ctx.box).values():
            ctx.log.add_secret(value)
        ctx.log.write(t("wp.dump_import", name=dump.name, db=database.name, size=f"{dump.stat().st_size / 1024 ** 2:.1f}"))
        database.import_status, database.error = "running", None
        ctx.db.commit()
        try:
            gz, fmt = dbs.detect_format(dump, dump.name)
            dbs.check_format(database, fmt)
            ctx.docker.exec_stdin(database.container, dbs.import_cmd(database, fmt),
                                  dbs.read_backup(dump, gz, database.engine), ctx.log, dbs.IMPORT_TIMEOUT)
            database.import_status = "success"
            ctx.log.write(t("wp.dump_done"))
        except Exception as e:  # noqa: BLE001 - lỗi import không chặn deploy
            database.import_status, database.error = "failed", ctx.log.redact(str(e))[:2000]
            ctx.log.write(t("wp.dump_failed", error=e))
        ctx.db.commit()
    finally:
        shutil.rmtree(dumps[0].parent, ignore_errors=True)


def rollback(ctx: JobContext) -> None:
    from . import compose

    source = ctx.db.get(Job, ctx.job.source_job_id) if ctx.job.source_job_id else None
    if source is None or source.service_id != ctx.service.id or not source.image:
        raise RuntimeError(t("deploy.rollback_not_found"))
    if ctx.settings.compose_snapshot(ctx.service.id, source.id).is_file():
        compose.rollback(ctx, source)
        return
    if not ctx.docker.image_exists(source.image):
        raise RuntimeError(t("deploy.rollback_image_pruned", image=source.image, job_id=source.id))
    ctx.log.write(t("deploy.rollback", job_id=source.id, commit=source.commit_sha or t("deploy.unknown_commit")))
    ctx.job.image = source.image
    ctx.job.commit_sha, ctx.job.commit_message = source.commit_sha, source.commit_message
    ctx.db.commit()
    wait_for_databases(ctx)
    release(ctx, source.image)


def restore_data(ctx: JobContext) -> None:
    from . import service_backups

    service_backups.restore(ctx)


PIPELINES: dict[str, Pipeline] = {
    JobKind.DEPLOY: deploy,
    JobKind.ROLLBACK: rollback,
    JobKind.RESTORE: restore_data,
}
