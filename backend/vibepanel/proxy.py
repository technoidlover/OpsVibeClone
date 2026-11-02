"""Traefik: reverse proxy + SSL Let's Encrypt cho panel và các app.

Traefik không đọc label hay docker.sock. Nó hỏi panel qua HTTP provider
(/api/internal/traefik) mỗi 5 giây, nên panel là nguồn sự thật duy nhất về
route: đổi tên miền hay chuyển sang container mới chỉ cần cập nhật DB.
"""

import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import panel_settings
from .config import Settings
from .docker import Docker
from .models import Job, Service

logger = logging.getLogger(__name__)

CERT_RESOLVER = "le"
CONFIG_LABEL = "opsvibes.config"
ACME_VOLUME = "opsvibes-acme"


def traefik_args(settings: Settings, token: str, acme_email: str | None) -> list[str]:
    endpoint = f"http://{settings.panel_container}:3000/api/internal/traefik?token={token}"
    args = [
        "--entrypoints.web.address=:80",
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
        "--entrypoints.websecure.address=:443",
        # Mặc định Traefik v3 cắt request đọc quá 60 giây: upload file nén (tới 5 GB) hay backup
        # database qua mạng chậm sẽ đứt giữa chừng
        "--entrypoints.websecure.transport.respondingTimeouts.readTimeout=3h",
        f"--providers.http.endpoint={endpoint}",
        "--providers.http.pollInterval=5s",
        f"--certificatesresolvers.{CERT_RESOLVER}.acme.httpchallenge=true",
        f"--certificatesresolvers.{CERT_RESOLVER}.acme.httpchallenge.entrypoint=web",
        f"--certificatesresolvers.{CERT_RESOLVER}.acme.storage=/acme/acme.json",
        "--log.level=INFO",
    ]
    if acme_email:
        args.append(f"--certificatesresolvers.{CERT_RESOLVER}.acme.email={acme_email}")
    return args


def ensure_traefik(docker: Docker, settings: Settings, db: Session) -> None:
    """Tạo hoặc cập nhật container Traefik khi cấu hình thay đổi."""
    docker.ensure_network()
    args = traefik_args(settings, panel_settings.traefik_token(db), panel_settings.get(db, panel_settings.ACME_EMAIL))
    config_hash = hashlib.sha256(json.dumps([settings.traefik_image, args]).encode()).hexdigest()[:16]
    name = settings.traefik_container

    if docker.container_label(name, CONFIG_LABEL) == config_hash:
        if not docker.is_running(name):
            docker.start(name)
        return

    logger.info("Khởi tạo Traefik (%s)", settings.traefik_image)
    docker.remove_container(name)
    docker.run_raw(
        [
            "-d",
            "--name", name,
            "--network", settings.network,
            "--restart", "unless-stopped",
            "-p", "80:80",
            "-p", "443:443",
            "-v", f"{ACME_VOLUME}:/acme",
            "--label", f"{CONFIG_LABEL}={config_hash}",
            "--log-opt", "max-size=10m",
            settings.traefik_image,
            *args,
        ]
    )


def _host_rule(hosts: list[str]) -> str:
    # Tên miền đã qua validator nên không chứa dấu backtick
    return " || ".join(f"Host(`{h}`)" for h in hosts)


def dynamic_config(db: Session, settings: Settings) -> dict:
    routers: dict[str, dict] = {}
    services: dict[str, dict] = {}

    def add(name: str, hosts: list[str], url: str) -> None:
        routers[name] = {
            "rule": _host_rule(hosts),
            "entryPoints": ["websecure"],
            "service": name,
            "tls": {"certResolver": CERT_RESOLVER},
        }
        services[name] = {"loadBalancer": {"servers": [{"url": url}]}}

    panel_domain = panel_settings.get(db, panel_settings.PANEL_DOMAIN)
    if panel_domain:
        add("opsvibes", [panel_domain], f"http://{settings.panel_container}:3000")

    base_domain = panel_settings.get(db, panel_settings.BASE_DOMAIN)
    rows = db.execute(
        select(Service, Job).join(Job, Job.id == Service.active_job_id).where(Job.container.is_not(None))
    ).all()
    for service, job in rows:
        hosts = [h for h in (service.domain, default_domain(service, base_domain)) if h]
        if hosts and job.port:
            add(f"svc-{service.id}", hosts, f"http://{job.container}:{job.port}")

    return {"http": {"routers": routers, "services": services}} if routers else {}


def default_domain(service: Service, base_domain: str | None) -> str | None:
    return panel_settings.default_service_domain(base_domain, service.project.name, service.name)
