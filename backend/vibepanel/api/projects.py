import json
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError

from .. import (archive, backups, cleanup, compose, github, panel_settings, project_backups, service_backups, site_files,
               storage, templates)
from ..docker import Docker, DockerError
from ..i18n import t
from ..assistant.session import AGY_BRAIN, BrainError, resolve_brain
from ..models import Job, JobKind, JobStatus, JobTrigger, Project, Service
from ..pipelines import load_env
from ..proxy import default_domain
from ..validators import Branch, Domain, EnvVars, ImageRef, Mounts, Name, RepoUrl, RootDir, StartCommand
from .deps import DB, AppSettings, Box, CurrentUser, DockerDep, ProjectDep, Queue, ServiceDep
from . import databases as db_api
from .databases import remove_runtime as remove_databases
from .jobs import JobOut

router = APIRouter(prefix="/api/projects", tags=["projects"])

# ---------- Schemas ----------

# Model AI tự sửa cấu hình khi deploy lỗi ("provider:<id>", như AI Agent); "none" = không dùng AI
AgentChoice = Annotated[str, Field(max_length=40)]
# auto: Dockerfile nếu có, không thì Nixpacks; compose: chạy compose.yml
BuildMode = Literal["auto", "compose"]
HealthCheck = Literal["tcp", "http", "none"]
# Đường dẫn health check HTTP, vd /health
HealthPath = Annotated[str, Field(max_length=200, pattern=r"^/[!-~]*$")]
# Tên service trong compose.yml; "" = tự chọn
ComposeService = Annotated[str, Field(max_length=63, pattern=r"^([A-Za-z0-9][A-Za-z0-9._-]*)?$")]

class ProjectCreate(BaseModel):
    name: Name


class ProjectOut(BaseModel):
    name: str
    created_at: datetime
    service_count: int
    database_count: int = 0


class GitHubSource(BaseModel):
    installation_id: int
    repo_id: int


class DatabaseSpec(BaseModel):
    """Có `engine`: tạo database mới (tên tự đặt nếu bỏ trống). Không có: dùng database `name` có sẵn."""

    engine: Literal["postgres", "mysql", "mariadb", "mongo", "redis"] | None = None
    version: str | None = Field(None, max_length=20)
    name: Name | None = None


class ServiceCreate(BaseModel):
    """Nguồn là repo chọn qua GitHub App (`github`), URL Git nhập tay (`repo_url`), image Docker (`image`),
    file nén (`archive` = true, tải file lên sau qua /archive) hoặc tự viết (`manual` = true: Dockerfile /
    compose.yml... soạn qua /files)."""

    name: Name
    repo_url: RepoUrl | None = None
    github: GitHubSource | None = None
    image: ImageRef | None = None
    archive: bool = False
    manual: bool = False
    build_mode: BuildMode = "auto"
    health_check: HealthCheck = "tcp"
    health_path: HealthPath | None = None
    compose_service: ComposeService | None = None
    # Thư mục con chứa app trong repo / file nén (monorepo)
    root_dir: RootDir | None = None
    # Thư mục trong container giữ dữ liệu qua các lần deploy bằng Docker volume (file upload, SQLite...)
    volumes: Mounts = Field(default_factory=list)
    branch: Branch | None = None
    domain: Domain | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    auto_deploy: bool = True
    git_token: str | None = Field(default=None, max_length=500)
    start_command: StartCommand | None = None
    deploy_agent: AgentChoice | None = None
    env: EnvVars = Field(default_factory=dict)
    # Database tạo kèm hoặc dùng chung; biến kết nối được thêm vào env, deploy chờ chúng sẵn sàng
    databases: list[DatabaseSpec] = Field(default_factory=list, max_length=3)


class ServiceUpdate(BaseModel):
    """Trường bỏ trống (null) giữ nguyên giá trị cũ. git_token = "" để xóa token."""

    repo_url: RepoUrl | None = None
    branch: Branch | None = None
    domain: Domain | None = None
    clear_domain: bool = False
    port: int | None = Field(default=None, ge=1, le=65535)
    clear_port: bool = False
    auto_deploy: bool | None = None
    git_token: str | None = Field(default=None, max_length=500)
    # "" = xóa, quay về tự nhận diện
    start_command: StartCommand | None = None
    deploy_agent: AgentChoice | None = None
    env: EnvVars | None = None
    # Chỉ với service dạng image
    image: ImageRef | None = None
    # Thư mục giữ dữ liệu (mọi loại service; service compose khai báo volume trong compose.yml)
    volumes: Mounts | None = None
    # "" = thư mục gốc
    root_dir: RootDir | None = None
    build_mode: BuildMode | None = None
    compose_service: ComposeService | None = None
    health_check: HealthCheck | None = None
    health_path: HealthPath | None = None


class ServiceOut(BaseModel):
    project: str
    name: str
    repo_url: str
    # owner/repo khi service gắn với GitHub App (webhook tự hoạt động)
    github_repo: str | None
    # git | image | archive | manual; archive_name = file nén đang dùng
    source: str
    build_mode: str
    compose_service: str | None
    health_check: str
    health_path: str | None
    archive_name: str | None
    root_dir: str | None
    # Host nội bộ để service khác gọi tới (${tên.HOST})
    internal_host: str
    # Service chạy thẳng image Docker (không có repo); template = ứng dụng 1-click
    image: str | None
    template: str | None
    volumes: list[str]
    # Database đã liên kết (deploy chờ chúng sẵn sàng)
    databases: list[str]
    branch: str
    domain: str | None
    port: int | None
    auto_deploy: bool
    start_command: str | None
    deploy_agent: str | None
    has_git_token: bool
    env: dict[str, str]
    webhook_path: str
    active_job_id: int | None
    default_domain: str | None
    urls: list[str]
    created_at: datetime
    updated_at: datetime


class WebhookInfo(BaseModel):
    path: str
    secret: str
    content_type: str = "application/json"
    events: list[str] = ["push"]


# ---------- Helpers ----------

class RollbackRequest(BaseModel):
    job_id: int


class DeployRequest(BaseModel):
    # Deploy kèm thay đổi do AI Agent tạo (chưa có trong repo)
    patch_job_id: int | None = None
    # AI tự sửa cấu hình khi deploy lỗi; được lưu làm mặc định của service (cả deploy do push)
    agent: AgentChoice | None = None
    # AI Agent đọc mã nguồn và chuẩn bị cấu hình / biến môi trường trước khi build
    prepare: bool = False


class ContainerLogs(BaseModel):
    container: str | None
    logs: str


# Dòng lỗi app ghi ra stdout (nginx error_log trỏ /dev/stdout, PHP, Python, log có mức ERROR...)
_ERROR_LINE = re.compile(
    r"\[(error|crit|alert|emerg)\]|PHP (Fatal|Parse|Warning|Notice|Deprecated)|Traceback \(most recent|"
    r"\b(ERROR|FATAL|CRITICAL|Exception|Uncaught|panic:)\b|\blevel=(error|fatal)\b|\"level\":\s*\"?(error|fatal|50|60)\b"
)


def filter_logs(lines: list[tuple[str, str]], stream: str) -> str:
    """all: mọi dòng; stdout: output thường (access log...); stderr: luồng lỗi + dòng trông như lỗi ở stdout."""
    if stream == "stdout":
        picked = [line for s, line in lines if s == "stdout"]
    elif stream == "stderr":
        picked = [line for s, line in lines if s == "stderr" or _ERROR_LINE.search(line)]
    else:
        picked = [line for _, line in lines]
    return "".join(line + "\n" for line in picked)


def service_out(service: Service, box: Box, db: DB) -> ServiceOut:
    auto_domain = default_domain(service, panel_settings.get(db, panel_settings.BASE_DOMAIN))
    hosts = [h for h in (service.domain, auto_domain) if h]
    return ServiceOut(
        project=service.project.name,
        name=service.name,
        repo_url=service.repo_url,
        github_repo=service.github_repo,
        source=service.source,
        build_mode=service.build_mode,
        compose_service=service.compose_service,
        health_check=service.health_check,
        health_path=service.health_path,
        archive_name=service.archive_name,
        root_dir=service.root_dir,
        internal_host=service.internal_host,
        image=service.image,
        template=service.template,
        volumes=service.volumes,
        databases=sorted(d.name for d in service.databases),
        branch=service.branch,
        domain=service.domain,
        port=service.port,
        auto_deploy=service.auto_deploy,
        start_command=service.start_command,
        deploy_agent=service.deploy_agent,
        has_git_token=service.git_token_enc is not None,
        env=load_env(service, box),
        webhook_path=f"/api/webhooks/github/{service.id}",
        active_job_id=service.active_job_id,
        default_domain=auto_domain,
        urls=[f"https://{h}" for h in hosts] if service.active_job_id else [],
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


def _agent_choice(db: DB, box: Box, choice: str | None) -> str | None:
    """Kiểm tra model AI dùng được trước khi lưu làm lựa chọn tự sửa khi deploy của service."""
    if choice in (None, "", "none"):
        return None
    # Antigravity CLI: kiểm tra kết nối lúc deploy, không resolve như model API key
    if choice == AGY_BRAIN:
        return choice
    try:
        resolve_brain(db, box, choice, None)
    except BrainError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    return choice


def _dump_env(env: dict[str, str], box: Box) -> str | None:
    return box.encrypt(json.dumps(env)) if env else None


def _has_active_jobs(db: DB, *service_ids: int) -> bool:
    if not service_ids:
        return False
    return db.scalar(
        select(
            exists().where(
                Job.service_id.in_(service_ids),
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
        )
    )


def _remove_runtime(docker: Docker | None, services: list[Service], keep_data: bool = False) -> None:
    """Gỡ container, image và volume dữ liệu của service (bỏ qua nếu panel không điều khiển Docker)."""
    if docker is None:
        return
    for service in services:
        try:
            for name in docker.containers_for_service(service.id):
                docker.remove_container(name)
        except DockerError:
            pass
        # Image kéo về (ứng dụng 1-click) có thể đang được service khác dùng: docker tự từ chối xóa
        for image in {j.image for j in service.jobs if j.image}:
            docker.remove_image(image)
        if not keep_data:
            for mount in service.volumes:
                docker.remove_volume(templates.volume_name(service.slug, mount))
            if service.site_volume:
                docker.remove_volume(service.site_volume)


def _commit_or_conflict(db: DB, message: str) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, message)


# ---------- Projects ----------

@router.get("")
def list_projects(db: DB, _user: CurrentUser) -> list[ProjectOut]:
    projects = db.scalars(select(Project).order_by(Project.name)).all()
    return [ProjectOut(name=p.name, created_at=p.created_at, service_count=len(p.services),
                       database_count=len(p.databases)) for p in projects]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_project(req: ProjectCreate, db: DB, _user: CurrentUser) -> ProjectOut:
    project = Project(name=req.name)
    db.add(project)
    _commit_or_conflict(db, t("project.name_taken"))
    return ProjectOut(name=project.name, created_at=project.created_at, service_count=0)


@router.delete("/{project}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project: ProjectDep, db: DB, settings: AppSettings, docker: DockerDep, queue: Queue,
                   request: Request) -> None:
    services = list(project.services)
    if _has_active_jobs(db, *(s.id for s in services)):
        raise HTTPException(status.HTTP_409_CONFLICT, t("project.jobs_running"))
    if any(d.import_status == "running" for d in project.databases):
        raise HTTPException(status.HTTP_409_CONFLICT, t("project.import_running"))
    if project_backups.is_running(project):
        raise HTTPException(status.HTTP_409_CONFLICT, t("pbackup.restore_running"))
    _remove_runtime(docker, services)
    for service in services:
        if docker is not None:
            compose.remove(docker, settings, service, keep_data=False)
        shutil.rmtree(settings.files_dir(service.id), ignore_errors=True)
        service_backups.remove_all(settings, service)
    remove_databases(docker, list(project.databases))
    for database in project.databases:
        backups.remove_all(settings, database)
    project_backups.remove_all(settings, project)
    workspaces = [settings.workspaces_dir / s.slug for s in services]
    job_ids = [j.id for s in services for j in s.jobs]
    leftovers = ([settings.archive_path(s.id) for s in services] + [site_files.fresh_marker(settings, s) for s in services]
                 + [db_api.log_path(settings, d) for d in project.databases])
    db.delete(project)
    db.commit()
    for path in workspaces:
        shutil.rmtree(path, ignore_errors=True)
    for path in leftovers:
        path.unlink(missing_ok=True)
    queue.remove_files(*job_ids)
    cleanup.run_in_background(docker, request.app.state.sessions, settings)


@router.get("/{project}/storage")
def project_storage(project: ProjectDep, docker: DockerDep, settings: AppSettings, refresh: bool = False) -> dict:
    """Dung lượng ổ đĩa của từng service / database và của cả project (xem storage.py)."""
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    try:
        return storage.project_storage(project, docker, settings, refresh)
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None


# ---------- Services ----------

@router.get("/{project}/services")
def list_services(project: ProjectDep, box: Box, db: DB) -> list[ServiceOut]:
    return [service_out(s, box, db) for s in sorted(project.services, key=lambda s: s.name)]


@router.post("/{project}/services", status_code=status.HTTP_201_CREATED)
def create_service(
    req: ServiceCreate, project: ProjectDep, db: DB, box: Box, docker: DockerDep, settings: AppSettings,
    request: Request,
) -> ServiceOut:
    repo_url, branch, gh = req.repo_url, req.branch, None
    if req.image is not None or req.archive or req.manual:
        repo_url, branch = "", None
    elif req.github is not None:
        app = github.get_app(db)
        if app is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("github.not_connected"))
        try:
            # Hỏi lại GitHub thay vì tin dữ liệu client: App phải còn quyền với repo này
            gh = github.repo(app, box, req.github.installation_id, req.github.repo_id)
        except github.GitHubError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("github.repo_unreachable", error=e)) from None
        repo_url, branch = gh.clone_url, branch or gh.default_branch
    elif repo_url is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("service.source_required"))
    service = new_service(
        db, box, project, req, repo_url=repo_url, branch=branch, gh=gh,
        deploy_agent=None if req.image else _agent_choice(db, box, req.deploy_agent),
    )
    created = []
    for spec in req.databases:
        if spec.engine:
            if docker is None:
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
            base = f"{req.name}-{'redis' if spec.engine == 'redis' else 'db'}"
            name = spec.name or db_api.free_name(project, base)
            database = db_api.new_database(db, project, spec.engine, spec.version, name, box)
            created.append(database)
        else:
            database = next((d for d in project.databases if d.name == spec.name), None)
            if database is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, t("database.not_found_named", name=spec.name))
        db_api.link(service, database, box)
    _commit_or_conflict(db, t("service.name_or_domain_taken"))
    for database in created:
        db_api.start(request, box, docker, settings, database)
    return service_out(service, box, db)


def new_service(db: DB, box: Box, project: Project, req: ServiceCreate, *, repo_url: str, branch: str | None,
                gh=None, deploy_agent: str | None = None, template: str | None = None) -> Service:
    """Thêm bản ghi service (chưa commit)."""
    if any(d.name == req.name for d in project.databases):
        raise HTTPException(status.HTTP_409_CONFLICT, t("service.name_used_by_database", name=req.name))
    service = Service(
        project=project,
        name=req.name,
        repo_url=repo_url,
        branch=branch or "main",
        github_installation_id=gh.installation_id if gh else None,
        github_repo_id=gh.id if gh else None,
        github_repo=gh.full_name if gh else None,
        domain=req.domain,
        port=req.port,
        auto_deploy=req.auto_deploy,
        start_command=None if req.image else (req.start_command or None),
        deploy_agent=deploy_agent,
        git_token_enc=None if req.image else box.encrypt_optional(req.git_token),
        webhook_secret_enc=box.encrypt(secrets.token_hex(32)),
        env_enc=_dump_env(req.env, box),
        image=req.image,
        template=template,
        source="image" if req.image else "archive" if req.archive else "manual" if req.manual else "git",
        root_dir=None if req.image or req.manual else (req.root_dir or None),
        build_mode="auto" if req.image else req.build_mode,
        compose_service=None if req.image else (req.compose_service or None),
        health_check=req.health_check,
        health_path=req.health_path,
    )
    service.volumes = req.volumes
    db.add(service)
    return service


@router.get("/{project}/services/{service}")
def get_service(service: ServiceDep, box: Box, db: DB) -> ServiceOut:
    return service_out(service, box, db)


@router.patch("/{project}/services/{service}")
def update_service(req: ServiceUpdate, service: ServiceDep, db: DB, box: Box) -> ServiceOut:
    if req.volumes is not None:
        service.volumes = req.volumes
    if service.image:
        if req.image is not None:
            service.image = req.image
    elif req.repo_url is not None and req.repo_url != service.repo_url:
        # Đổi sang repo khác bằng URL: bỏ liên kết GitHub App, dùng token nhập tay
        service.repo_url = req.repo_url
        service.github_installation_id = service.github_repo_id = service.github_repo = None
    if req.branch is not None:
        service.branch = req.branch
    if req.clear_domain:
        service.domain = None
    elif req.domain is not None:
        service.domain = req.domain
    if req.clear_port:
        service.port = None
    elif req.port is not None:
        service.port = req.port
    if req.auto_deploy is not None:
        service.auto_deploy = req.auto_deploy
    if req.git_token is not None:
        service.git_token_enc = box.encrypt_optional(req.git_token)
    if req.start_command is not None:
        service.start_command = req.start_command or None
    if req.deploy_agent is not None and not service.image:
        service.deploy_agent = _agent_choice(db, box, req.deploy_agent)
    if req.env is not None:
        service.env_enc = _dump_env(req.env, box)
    if req.root_dir is not None and not service.image and service.source != "manual":
        service.root_dir = req.root_dir or None
    if req.build_mode is not None and not service.image:
        service.build_mode = req.build_mode
    if req.compose_service is not None and not service.image:
        service.compose_service = req.compose_service or None
    if req.health_check is not None:
        service.health_check = req.health_check
    if req.health_path is not None:
        service.health_path = req.health_path if req.health_path != "/" else None
    _commit_or_conflict(db, t("service.domain_taken"))
    return service_out(service, box, db)


@router.delete("/{project}/services/{service}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service(service: ServiceDep, db: DB, settings: AppSettings, docker: DockerDep, queue: Queue,
                   request: Request, keep_data: bool = False) -> None:
    if _has_active_jobs(db, service.id):
        raise HTTPException(status.HTTP_409_CONFLICT, t("service.jobs_running"))
    _remove_runtime(docker, [service], keep_data)
    if docker is not None:
        compose.remove(docker, settings, service, keep_data)
    shutil.rmtree(settings.files_dir(service.id), ignore_errors=True)
    if not keep_data:
        service_backups.remove_all(settings, service)
    workspace = settings.workspaces_dir / service.slug
    upload = settings.archive_path(service.id)
    job_ids = [j.id for j in service.jobs]
    if keep_data and (service.volumes or service.site_volume):
        cleanup.keep_volumes(db, [templates.volume_name(service.slug, m) for m in service.volumes]
                             + ([service.site_volume] if service.site_volume else []))
    db.delete(service)
    db.commit()
    shutil.rmtree(workspace, ignore_errors=True)
    upload.unlink(missing_ok=True)
    upload.with_suffix(".fresh").unlink(missing_ok=True)
    queue.remove_files(*job_ids)
    cleanup.run_in_background(docker, request.app.state.sessions, settings)


@router.post("/{project}/services/{service}/deploy", status_code=status.HTTP_202_ACCEPTED)
def deploy_service(service: ServiceDep, db: DB, queue: Queue, box: Box, req: DeployRequest | None = None) -> JobOut:
    if service.source == "archive" and not queue.settings.archive_path(service.id).is_file():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.upload_first"))
    if service.image and req and (req.agent not in (None, "none") or req.patch_job_id is not None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("service.image_no_ai"))
    if req and req.agent is not None and not service.image:
        service.deploy_agent = _agent_choice(db, box, req.agent)
        db.commit()
    patch = None
    if req and req.patch_job_id is not None:
        patch = db.get(Job, req.patch_job_id)
        if patch is None or patch.service_id != service.id or not queue.settings.patch_path(patch.id).is_file():
            raise HTTPException(status.HTTP_404_NOT_FOUND, t("service.no_agent_patch"))
    if req and req.prepare and not service.deploy_agent:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("service.prepare_needs_ai"))
    job = queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.MANUAL, patch=patch,
                        prepare=bool(req and req.prepare))
    return JobOut.from_job(job)


def _check_upload(settings, size: int) -> None:
    if size > archive.MAX_UPLOAD:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            t("archive.upload_too_large", limit=archive.MAX_UPLOAD // archive.GB))
    try:
        archive.check_space(settings.data_dir / "archives", size)
    except archive.ArchiveError as e:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e)) from None


@router.get("/{project}/services/{service}/archive/check", status_code=status.HTTP_204_NO_CONTENT)
def check_archive_upload(service: ServiceDep, settings: AppSettings, size: int = Query(ge=0)) -> None:
    """Hỏi trước khi tải: file `size` byte có vượt giới hạn hoặc làm đầy ổ đĩa không."""
    _check_upload(settings, size)


@router.post("/{project}/services/{service}/archive", status_code=status.HTTP_202_ACCEPTED)
async def upload_archive(
    request: Request, service: ServiceDep, db: DB, queue: Queue, settings: AppSettings,
    filename: str = Query("source.zip", max_length=255), deploy: bool = True,
) -> JobOut | None:
    """Tải lên phiên bản mới (.zip, .tar.gz...) cho service nguồn file nén, rồi deploy.

    Thân request là nội dung file (application/octet-stream), không dùng multipart:
    multipart khiến file vài GB bị ghi ra file tạm trước rồi mới chép, tốn gấp đôi ổ đĩa.
    """
    if service.source != "archive":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.not_archive_service"))
    target = settings.archive_path(service.id)
    target.parent.mkdir(parents=True, exist_ok=True)
    too_large = HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                              t("archive.upload_too_large", limit=archive.MAX_UPLOAD // archive.GB))
    # Biết trước kích thước: báo vượt giới hạn / thiếu dung lượng ngay, không đợi tải xong
    _check_upload(settings, int(request.headers.get("content-length") or 0))
    temp = target.with_name(f"{service.id}-{time.time_ns()}.upload")
    size = checked = 0
    try:
        with temp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > archive.MAX_UPLOAD:
                    raise too_large
                # Không có Content-Length (hoặc ổ đĩa đầy dần vì việc khác): kiểm tra lại mỗi 256 MB
                if size - checked >= 256 * 1024 ** 2:
                    checked = size
                    if archive.free_space(target.parent) < 0:
                        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE,
                                            t("archive.no_space_upload", reserve=archive.gb(archive.RESERVE)))
                out.write(chunk)
        if size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.upload_empty"))
        if archive.detect(temp) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("archive.unsupported"))
        # Đang có job dùng file cũ: thay bằng rename (nguyên tử), job đó vẫn đọc được inode cũ
        temp.replace(target)
        # Website PHP chạy từ volume mã nguồn: lần deploy tới dựng lại volume từ bản mới này
        site_files.fresh_marker(settings, service).touch()
    finally:
        temp.unlink(missing_ok=True)
    service.archive_name = Path(filename).name[:255] or "source.zip"
    db.commit()
    if not deploy:
        return None
    return JobOut.from_job(queue.enqueue(db, service, JobKind.DEPLOY, JobTrigger.MANUAL))


@router.post("/{project}/services/{service}/rollback", status_code=status.HTTP_202_ACCEPTED)
def rollback_service(req: RollbackRequest, service: ServiceDep, db: DB, queue: Queue) -> JobOut:
    source = db.get(Job, req.job_id)
    if source is None or source.service_id != service.id or not source.image or source.status != JobStatus.SUCCESS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, t("service.rollback_invalid"))
    job = queue.enqueue(db, service, JobKind.ROLLBACK, JobTrigger.MANUAL, source=source)
    return JobOut.from_job(job)


@router.get("/{project}/services/{service}/logs")
def container_logs(
    service: ServiceDep, db: DB, docker: DockerDep, tail: int = Query(200, ge=10, le=5000),
    stream: Literal["all", "stdout", "stderr"] = "all",
) -> ContainerLogs:
    """Log runtime của container đang phục vụ traffic."""
    active = db.get(Job, service.active_job_id) if service.active_job_id else None
    if active is None or not active.container:
        return ContainerLogs(container=None, logs="")
    if docker is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, t("docker.unavailable"))
    try:
        # Tab lỗi lấy rộng hơn rồi lọc, để vẫn đủ dòng khi phần lớn log là access log
        lines = docker.log_lines(active.container, tail * 5 if stream == "stderr" else tail)
        text = filter_logs(lines, stream)
        if stream == "stderr":
            text = "".join(text.splitlines(keepends=True)[-tail:])
        return ContainerLogs(container=active.container, logs=text)
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e))


@router.get("/{project}/services/{service}/jobs")
def list_service_jobs(service: ServiceDep, db: DB, queue: Queue, limit: int = 20) -> list[JobOut]:
    """Deploy và rollback (job mang patch của AI Agent không hiện ở đây)."""
    limit = max(1, min(limit, 100))
    jobs = db.scalars(
        select(Job).where(Job.service_id == service.id, Job.kind != JobKind.AGENT).order_by(Job.id.desc()).limit(limit)
    ).all()
    out = [JobOut.from_job(j) for j in jobs]
    for item in out:
        # Patch cấu hình do AI tự sửa lúc deploy (tải về để commit)
        item.has_patch = queue.settings.patch_path(item.id).is_file()
    return out


@router.get("/{project}/services/{service}/webhook")
def get_webhook(service: ServiceDep, box: Box) -> WebhookInfo:
    return WebhookInfo(path=f"/api/webhooks/github/{service.id}", secret=box.decrypt(service.webhook_secret_enc))


@router.post("/{project}/services/{service}/webhook/rotate")
def rotate_webhook_secret(service: ServiceDep, db: DB, box: Box) -> WebhookInfo:
    secret = secrets.token_hex(32)
    service.webhook_secret_enc = box.encrypt(secret)
    db.commit()
    return WebhookInfo(path=f"/api/webhooks/github/{service.id}", secret=secret)
