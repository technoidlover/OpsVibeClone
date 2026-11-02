from fastapi import APIRouter, Query, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from .. import __version__
from ..models import Job, JobKind, JobStatus, Project, Service
from .deps import DB, AppSettings, Box, CurrentUser
from .jobs import JobOut
from .projects import ServiceOut, service_out

router = APIRouter(prefix="/api", tags=["dashboard"])


class ServiceSummary(BaseModel):
    service: ServiceOut
    latest_job: JobOut | None


class Dashboard(BaseModel):
    projects: int
    services: int
    running: int
    deploying: int
    items: list[ServiceSummary]


class CpuStat(BaseModel):
    percent: float
    cores: int
    load: list[float]


class UsageStat(BaseModel):
    total: int
    used: int
    percent: float


class NetworkStat(BaseModel):
    rx_rate: int  # byte/giây
    tx_rate: int


class ServerHistory(BaseModel):
    cpu: list[float]
    memory: list[float]
    disk: list[float]
    net_in: list[int]
    net_out: list[int]


class ServerStats(BaseModel):
    """Trường None khi chưa đo được (vừa khởi động, hoặc chạy dev trên macOS)."""

    cpu: CpuStat | None = None
    memory: UsageStat | None = None
    disk: UsageStat | None = None
    network: NetworkStat | None = None
    uptime_seconds: int | None = None
    interval: float
    history: ServerHistory


class PanelInfo(BaseModel):
    version: str
    ip: str | None


@router.get("/dashboard")
def dashboard(db: DB, box: Box, _user: CurrentUser, project: str | None = Query(None)) -> Dashboard:
    """Số service theo trạng thái và danh sách service kèm job gần nhất, trong một lần gọi."""
    query = select(Service).join(Project).order_by(Project.name, Service.name)
    if project:
        query = query.where(Project.name == project)
    services = db.scalars(query).all()
    ids = [s.id for s in services]

    latest_ids = db.scalars(
        select(func.max(Job.id))
        .where(Job.service_id.in_(ids), Job.kind != JobKind.AGENT)
        .group_by(Job.service_id)
    ).all() if ids else []
    latest = {j.service_id: j for j in db.scalars(select(Job).where(Job.id.in_(latest_ids)))} if latest_ids else {}

    return Dashboard(
        projects=db.scalar(select(func.count()).select_from(Project)) if not project else 1,
        services=len(services),
        running=sum(1 for s in services if s.active_job_id),
        deploying=sum(1 for j in latest.values() if j.status in (JobStatus.QUEUED, JobStatus.RUNNING)),
        items=[
            ServiceSummary(service=service_out(s, box, db), latest_job=JobOut.from_job(latest[s.id]) if s.id in latest else None)
            for s in services
        ],
    )


@router.get("/server")
def server_stats(request: Request, _user: CurrentUser) -> ServerStats:
    """CPU, RAM, ổ đĩa, mạng của máy chủ kèm lịch sử 5 phút (mẫu mỗi 5 giây)."""
    return ServerStats(**request.app.state.sampler.read())


@router.get("/info")
def panel_info(settings: AppSettings, _user: CurrentUser) -> PanelInfo:
    return PanelInfo(version=__version__, ip=settings.public_ip or None)
