"""Ứng dụng 1-click: chọn từ danh mục, panel tạo database (nếu cần), service và deploy luôn."""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from .. import panel_settings, templates
from ..i18n import t
from ..models import JobKind, JobTrigger
from ..validators import Domain, Name
from . import databases as db_api
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep, ProjectDep, Queue
from .projects import ServiceCreate, ServiceOut, _commit_or_conflict, new_service, service_out

router = APIRouter(tags=["templates"])


class TemplateOut(BaseModel):
    key: str
    label: str
    category: str
    description: str
    image: str
    port: int
    volumes: list[str]
    # vd "MariaDB 11.4"; None = không cần database
    database: str | None
    needs_url: bool
    website: str
    next_step: str


class TemplateCreate(BaseModel):
    template: str
    # Bỏ trống: đặt theo tên ứng dụng (wordpress, wordpress-2...)
    name: Name | None = None
    domain: Domain | None = None


class TemplateResult(BaseModel):
    service: ServiceOut
    job_id: int
    next_step: str


@router.get("/api/templates")
def list_templates(_user: CurrentUser) -> list[TemplateOut]:
    return [
        TemplateOut(key=tpl.key, label=tpl.label, category=t(tpl.category), description=t(tpl.description),
                    image=tpl.image, port=tpl.port, volumes=list(tpl.volumes), database=tpl.db_label,
                    needs_url=tpl.needs_url, website=tpl.website, next_step=t(tpl.next_step))
        for tpl in templates.TEMPLATES.values()
    ]


@router.post("/api/projects/{project}/templates", status_code=status.HTTP_201_CREATED)
def create_from_template(
    req: TemplateCreate, project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings,
    queue: Queue, request: Request,
) -> TemplateResult:
    tpl = templates.TEMPLATES.get(req.template)
    if tpl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("tpl.not_found"))
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("tpl.no_docker"))
    if tpl.needs_url and not req.domain and not panel_settings.get(db, panel_settings.BASE_DOMAIN):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            t("tpl.needs_url", app=tpl.label),
        )

    name = req.name or db_api.free_name(project, tpl.key)
    if any(s.name == name for s in project.services):
        raise HTTPException(status.HTTP_409_CONFLICT, t("tpl.service_exists", name=name))
    database = None
    if tpl.database:
        engine, version = tpl.database
        database = db_api.new_database(db, project, engine, version, db_api.free_name(project, f"{name}-db"), box)
    env = tpl.env(database, box.decrypt(database.password_enc) if database else "")
    spec = ServiceCreate(name=name, image=tpl.image, volumes=list(tpl.volumes), port=tpl.port, domain=req.domain,
                         auto_deploy=False, env=env)
    service = new_service(db, box, project, spec, repo_url="", branch=None, template=tpl.key)
    if database is not None:
        # Biến kết nối đã nằm trong env theo đúng tên ứng dụng cần; chỉ ghi nhận liên kết để deploy chờ DB
        service.databases.append(database)
    _commit_or_conflict(db, t("tpl.name_conflict"))

    if database is not None:
        db_api.start(request, box, docker, settings, database)
    job = queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.MANUAL)
    return TemplateResult(service=service_out(service, box, db), job_id=job.id, next_step=t(tpl.next_step))
