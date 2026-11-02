"""Tên miền trên toàn panel: tên miền riêng, tên miền mặc định của service và tên miền panel; kiểm tra DNS + SSL."""

import socket
import ssl
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import panel_settings
from ..models import Service
from ..proxy import default_domain
from .deps import DB, AppSettings, CurrentUser, DockerDep

router = APIRouter(prefix="/api/domains", tags=["domains"])

CHECK_TIMEOUT = 5


class DomainOut(BaseModel):
    domain: str
    # custom: tên miền riêng · default: tên miền mặc định (<project>-<service>.<tên miền gốc>) · panel
    kind: str
    project: str | None
    service: str | None
    # Service đã deploy thành công (Traefik đang route tên miền này)
    active: bool
    # Worker không nhận traffic: tên miền không được route
    routed: bool


class CheckRequest(BaseModel):
    domains: list[str] = Field(max_length=200)


class DnsResult(BaseModel):
    addresses: list[str]
    # Có trỏ về IP máy chủ không (None: panel không biết IP công khai)
    points_here: bool | None
    error: str | None


class SslResult(BaseModel):
    valid: bool
    issuer: str | None
    expires_at: datetime | None
    days_left: int | None
    error: str | None


class CheckOut(BaseModel):
    domain: str
    dns: DnsResult
    ssl: SslResult


@router.get("")
def list_domains(db: DB, _user: CurrentUser) -> list[DomainOut]:
    base = panel_settings.get(db, panel_settings.BASE_DOMAIN)
    out: list[DomainOut] = []
    panel = panel_settings.get(db, panel_settings.PANEL_DOMAIN)
    if panel:
        out.append(DomainOut(domain=panel, kind="panel", project=None, service=None, active=True, routed=True))
    services = db.scalars(select(Service)).all()
    for service in sorted(services, key=lambda s: (s.project.name, s.name)):
        routed = service.health_check != "none"
        for kind, host in (("custom", service.domain), ("default", default_domain(service, base))):
            if host:
                out.append(DomainOut(domain=host, kind=kind, project=service.project.name, service=service.name,
                                     active=service.active_job_id is not None, routed=routed))
    return out


def _dns(domain: str, public_ip: str) -> DnsResult:
    try:
        infos = socket.getaddrinfo(domain, None, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return DnsResult(addresses=[], points_here=False if public_ip else None, error=str(e))
    addresses = sorted({info[4][0] for info in infos})
    return DnsResult(addresses=addresses, points_here=(public_ip in addresses) if public_ip else None, error=None)


def _name(parts) -> str | None:
    fields = {k: v for rdn in parts or () for k, v in rdn}
    return fields.get("organizationName") or fields.get("commonName")


def _ssl(domain: str, host: str) -> SslResult:
    """Chứng chỉ Traefik trả cho tên miền này (kết nối thẳng Traefik, gửi SNI = tên miền)."""
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=CHECK_TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as e:
        # Thường là chứng chỉ tự ký mặc định của Traefik: Let's Encrypt chưa cấp được (DNS chưa trỏ đúng...)
        return SslResult(valid=False, issuer=None, expires_at=None, days_left=None, error=e.verify_message or str(e))
    except (OSError, ssl.SSLError) as e:
        return SslResult(valid=False, issuer=None, expires_at=None, days_left=None, error=str(e))
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
    days = (expires - datetime.now(timezone.utc)).days
    return SslResult(valid=True, issuer=_name(cert.get("issuer")), expires_at=expires, days_left=days, error=None)


@router.post("/check")
def check_domains(req: CheckRequest, settings: AppSettings, docker: DockerDep, _user: CurrentUser) -> list[CheckOut]:
    # Panel chạy trong Docker: hỏi thẳng container Traefik; không thì hỏi qua DNS công khai
    via = settings.traefik_container if docker is not None else None

    def one(domain: str) -> CheckOut:
        return CheckOut(domain=domain, dns=_dns(domain, settings.public_ip), ssl=_ssl(domain, via or domain))

    domains = list(dict.fromkeys(d.strip().lower() for d in req.domains if d.strip()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(one, domains))
