"""Deploy bằng docker compose: người dùng tự viết compose.yml (kèm Dockerfile, file .sql...).

Panel giải cấu hình (`docker compose config`), rồi chỉnh trước khi chạy:
- Service công khai được nối thêm vào network của panel (alias = host nội bộ của service),
  Traefik route tên miền tới nó như service thường; các service khác ở network riêng của project.
- Biến môi trường của service trong panel được truyền vào service công khai và dùng được trong
  compose.yml dạng ${TÊN}.
- Image tự build được gắn tag theo job, cấu hình đã giải lưu lại theo job: rollback và tự khôi
  phục bản cũ khi bản mới không lên chỉ cần `up --no-build` cấu hình cũ.

Khác deploy thường: compose tạo lại container tại chỗ (có gián đoạn ngắn), không chạy song song.
"""

import json
import os
import re

from sqlalchemy import select

from . import custom_files
from .docker import JOB_LABEL, MANAGED_LABEL, SERVICE_LABEL, DockerError
from .i18n import t
from .jobs import JobContext
from .models import Job, JobStatus, Service
from .pipelines import DEFAULT_PORT, IMAGE_HEALTH_TIMEOUT, load_env, register_env_secrets, render_env, wait_healthy

NETWORK_KEY = "opsvibes"
# Khóa network trong snapshot của các lần deploy trước khi đổi tên sản phẩm
LEGACY_NETWORK_KEY = "vibepanel"
# Traefik giữ cổng 80/443 của máy
RESERVED_HOST_PORTS = {"80", "443"}


def project_name(service: Service) -> str:
    return f"vp_{service.slug}".lower()


def _image_tag(service: Service, name: str, job_id: int) -> str:
    return f"vp/{service.slug}-{re.sub(r'[^a-z0-9_.-]', '-', name.lower())}:{job_id}"


def _ports(svc: dict) -> list[int]:
    out = []
    for p in svc.get("ports") or []:
        target = p.get("target") if isinstance(p, dict) else None
        if target:
            out.append(int(target))
    for e in svc.get("expose") or []:
        try:
            out.append(int(str(e).split("/")[0].split("-")[0]))
        except ValueError:
            pass
    return out


def pick_public(service: Service, services: dict[str, dict]) -> str:
    """Service trong compose nhận traffic: theo cài đặt, không thì đoán (có cổng / tự build / duy nhất)."""
    names = list(services)
    if service.compose_service:
        if service.compose_service not in services:
            raise RuntimeError(t("compose.public_missing", name=service.compose_service, names=", ".join(names)))
        return service.compose_service
    if len(names) == 1:
        return names[0]
    for candidates in ([n for n in names if _ports(services[n])], [n for n in names if services[n].get("build")]):
        if len(candidates) == 1:
            return candidates[0]
    raise RuntimeError(t("compose.public_ambiguous", names=", ".join(names)))


def prepare_config(ctx: JobContext, config: dict, public: str, env: dict[str, str], port: int | None) -> dict:
    """Chỉnh cấu hình đã giải: network panel, nhãn, biến môi trường, tag image theo job."""
    service, job = ctx.service, ctx.job
    services = config["services"]
    pub = services[public]
    if pub.get("network_mode"):
        raise RuntimeError(t("compose.network_mode", name=public))

    networks = config.setdefault("networks", {})
    key = NETWORK_KEY
    while key in networks and networks[key].get("name") != ctx.settings.network:
        key += "_"
    networks[key] = {"name": ctx.settings.network, "external": True}

    nets = pub.get("networks") or {"default": None}
    if "default" in nets:
        networks.setdefault("default", {"name": f"{project_name(service)}_default"})
    nets[key] = {"aliases": [service.internal_host]}
    pub["networks"] = nets
    environment = pub.get("environment") or {}
    pub["environment"] = {**({"PORT": str(port)} if port else {}), **environment, **env}

    for name, svc in services.items():
        svc["labels"] = {**(svc.get("labels") or {}), MANAGED_LABEL: "true", SERVICE_LABEL: str(service.id),
                         JOB_LABEL: str(job.id)}
        svc.setdefault("restart", "unless-stopped")
        svc.setdefault("logging", {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}})
        if svc.get("build"):
            svc["image"] = _image_tag(service, name, job.id)
        kept = []
        for p in svc.get("ports") or []:
            published = str(p.get("published") or "") if isinstance(p, dict) else ""
            if published.split("-")[0] in RESERVED_HOST_PORTS:
                ctx.log.write(t("compose.port_dropped", name=name, port=published))
            else:
                kept.append(p)
        if svc.get("ports"):
            svc["ports"] = kept
    return config


def _write_snapshot(ctx: JobContext, config: dict):
    path = ctx.settings.compose_snapshot(ctx.service.id, ctx.job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Chứa giá trị biến môi trường: chỉ panel đọc được
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return path


def _public_of(snapshot: dict) -> str | None:
    for name, svc in snapshot.get("services", {}).items():
        if any(n.startswith((NETWORK_KEY, LEGACY_NETWORK_KEY)) for n in (svc.get("networks") or {})):
            return name
    return None


def _load_snapshot(path, settings) -> dict:
    """Snapshot cũ có thể trỏ tới network trước khi đổi tên: luôn nối vào network hiện tại của panel."""
    config = json.loads(path.read_text())
    for net in (config.get("networks") or {}).values():
        if isinstance(net, dict) and net.get("external") and net.get("name") == LEGACY_NETWORK_KEY:
            net["name"] = settings.network
    return config


def deploy(ctx: JobContext) -> None:
    service, job, docker = ctx.service, ctx.job, ctx.docker
    file = custom_files.compose_file(ctx.build_dir)
    if file is None:
        raise RuntimeError(t("compose.missing"))
    project = project_name(service)
    env = render_env(ctx, load_env(service, ctx.box), warn=True)
    register_env_secrets(ctx.log, env)

    ctx.log.write(t("compose.config", file=file.name))
    try:
        config = docker.compose_config(project, file, env)
    except DockerError as e:
        raise RuntimeError(t("compose.invalid", error=e)) from None
    services = config.get("services") or {}
    if not services:
        raise RuntimeError(t("compose.no_services"))
    public = pick_public(service, services)
    found = _ports(services[public])
    port = service.port or (found[0] if found else None)
    config = prepare_config(ctx, config, public, env, port)
    snapshot = _write_snapshot(ctx, config)

    ctx.log.write(t("compose.up", project=project, public=public, services=", ".join(services)))
    docker.compose_up(project, snapshot, ctx.log, ctx.settings.build_timeout_seconds, build=True)
    _finish(ctx, project, public, port, config)


def _finish(ctx: JobContext, project: str, public: str, port: int | None, config: dict) -> None:
    service, job, docker = ctx.service, ctx.job, ctx.docker
    containers = docker.compose_containers(project, public)
    if not containers:
        raise RuntimeError(t("compose.no_container", name=public))
    container = containers[0]
    image = config["services"][public].get("image")
    if service.health_check == "none":
        port = None
    elif port is None:
        exposed = docker.exposed_ports(image) if image else []
        port = exposed[0] if len(exposed) == 1 else DEFAULT_PORT
    if port:
        ctx.log.write(t("compose.wait", container=container, port=port))
    if error := wait_healthy(ctx, container, port, IMAGE_HEALTH_TIMEOUT):
        _restore_previous(ctx, project)
        raise error

    job.container, job.port, job.image = container, port, image or project
    service.active_job_id = job.id
    ctx.db.commit()
    # Container chạy kiểu thường trước khi chuyển sang compose
    compose_names = set(docker.compose_containers(project))
    for name in docker.containers_for_service(service.id):
        if name not in compose_names:
            ctx.log.write(t("deploy.remove_old", name=name))
            docker.remove_container(name)
    prune(ctx)


def _restore_previous(ctx: JobContext, project: str) -> None:
    """Bản mới không lên: chạy lại cấu hình compose của bản đang phục vụ (nếu có)."""
    active = ctx.service.active_job_id
    path = ctx.settings.compose_snapshot(ctx.service.id, active) if active else None
    if path is None or not path.is_file() or active == ctx.job.id:
        return
    ctx.log.write(t("compose.restore", job_id=active))
    try:
        config = _load_snapshot(path, ctx.settings)
        if config != json.loads(path.read_text()):
            # Ghi đè tại chỗ: file đã có quyền 0600 từ lần deploy đó
            path.write_text(json.dumps(config, indent=2))
        ctx.docker.compose_up(project, path, ctx.log, ctx.settings.build_timeout_seconds, build=False)
    except Exception as e:  # noqa: BLE001 - lỗi khôi phục chỉ ghi log, lỗi chính vẫn được báo
        ctx.log.write(t("deploy.restart_old_failed", name=project, error=e))


def rollback(ctx: JobContext, source: Job) -> None:
    """Chạy lại cấu hình compose (và image đã build) của một lần deploy cũ. File gắn trực tiếp (bind mount)
    lấy theo mã nguồn hiện tại trong workspace."""
    service, docker = ctx.service, ctx.docker
    path = ctx.settings.compose_snapshot(service.id, source.id)
    config = _load_snapshot(path, ctx.settings)
    for svc in config.get("services", {}).values():
        image = svc.get("image") or ""
        if svc.get("build") and not docker.image_exists(image):
            raise RuntimeError(t("deploy.rollback_image_pruned", image=image, job_id=source.id))
    ctx.log.write(t("deploy.rollback", job_id=source.id, commit=source.commit_sha or t("deploy.unknown_commit")))
    ctx.job.commit_sha, ctx.job.commit_message = source.commit_sha, source.commit_message
    ctx.db.commit()
    public = _public_of(config)
    if public is None:
        raise RuntimeError(t("deploy.rollback_not_found"))
    # Lưu lại dưới job rollback để lần sau khôi phục / rollback tiếp được
    snapshot = _write_snapshot(ctx, config)
    project = project_name(service)
    docker.compose_up(project, snapshot, ctx.log, ctx.settings.build_timeout_seconds, build=False)
    _finish(ctx, project, public, source.port, config)


def prune(ctx: JobContext) -> None:
    """Giữ cấu hình + image của vài lần deploy thành công gần nhất (để rollback), dọn phần còn lại."""
    service, docker = ctx.service, ctx.docker
    folder = ctx.settings.compose_snapshot(service.id, 0).parent
    if not folder.is_dir():
        return
    ok = ctx.db.scalars(
        select(Job.id).where(Job.service_id == service.id, Job.status == JobStatus.SUCCESS).order_by(Job.id.desc())
    ).all()
    keep = {ctx.job.id}
    for job_id in ok:
        if len(keep) >= ctx.settings.keep_images:
            break
        if ctx.settings.compose_snapshot(service.id, job_id).is_file():
            keep.add(job_id)
    # Job đang chạy của service khác không đụng tới thư mục này; job lỗi gần đây cũng bị dọn
    prefix = f"vp/{service.slug}-"
    kept_images: set[str] = set()
    dropped: list[tuple[str, set[str]]] = []
    for path in folder.glob("*.json"):
        try:
            images = {s.get("image", "") for s in json.loads(path.read_text()).get("services", {}).values()}
        except (OSError, ValueError):
            images = set()
        images = {i for i in images if i.startswith(prefix)}
        if path.stem.isdigit() and int(path.stem) in keep:
            kept_images |= images
        else:
            dropped.append((str(path), images))
    for path, images in dropped:
        for image in images - kept_images:
            docker.remove_image(image)
        try:
            os.unlink(path)
        except OSError:
            pass


def remove(docker, settings, service: Service, keep_data: bool) -> None:
    """Gỡ compose project của service (khi xóa service)."""
    docker.compose_remove(project_name(service), volumes=not keep_data)
    folder = settings.compose_snapshot(service.id, 0).parent
    if folder.is_dir():
        for path in folder.glob("*.json"):
            try:
                images = {s.get("image", "") for s in json.loads(path.read_text()).get("services", {}).values()}
            except (OSError, ValueError):
                images = set()
            for image in images:
                if image.startswith(f"vp/{service.slug}-"):
                    docker.remove_image(image)
            path.unlink(missing_ok=True)
        if not any(folder.iterdir()):
            folder.rmdir()
