from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..i18n import t
from ..models import Job, JobStatus
from .deps import DB, AppSettings, CurrentUser, Queue

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# Giới hạn mỗi lần đọc log để một log build khổng lồ không làm nghẽn API
MAX_LOG_CHUNK = 256 * 1024


class JobOut(BaseModel):
    id: int
    project: str
    service: str
    kind: str
    status: str
    trigger: str
    branch: str
    commit_sha: str | None
    commit_message: str | None = None
    error: str | None
    agent: str | None
    prompt: str | None
    # Job deploy có áp patch do AI Agent tạo (job agent này)
    patch_job_id: int | None
    # Có patch để tải về (cấu hình AI đã sửa)
    has_patch: bool = False
    # Job deploy: model AI tự sửa cấu hình khi lỗi, và cuộc hội thoại ghi lại lần sửa đó
    heal_agent: str | None = None
    ai_prepare: bool = False
    assistant_session_id: int | None = None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_job(cls, job: Job) -> "JobOut":
        return cls(
            id=job.id,
            project=job.service.project.name,
            service=job.service.name,
            kind=job.kind,
            status=job.status,
            trigger=job.trigger,
            branch=job.branch,
            commit_sha=job.commit_sha,
            commit_message=job.commit_message,
            error=job.error,
            agent=job.agent,
            prompt=job.prompt,
            patch_job_id=job.patch_job_id,
            heal_agent=job.heal_agent,
            ai_prepare=job.ai_prepare,
            assistant_session_id=job.assistant_session_id,
            has_patch=False,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


class AgentResult(BaseModel):
    result: str | None
    diff: str | None


class JobLogChunk(BaseModel):
    content: str
    next_offset: int
    finished: bool


def _load_job(db: DB, job_id: int) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("job.not_found"))
    return job


@router.get("/{job_id}")
def get_job(job_id: int, db: DB, _user: CurrentUser) -> JobOut:
    return JobOut.from_job(_load_job(db, job_id))


@router.get("/{job_id}/result")
def get_agent_result(job_id: int, db: DB, _user: CurrentUser) -> AgentResult:
    """Câu trả lời và diff của job agent (tách riêng vì có thể rất dài)."""
    job = _load_job(db, job_id)
    return AgentResult(result=job.result, diff=job.diff)


@router.get("/{job_id}/patch")
def download_patch(job_id: int, db: DB, settings: AppSettings, _user: CurrentUser) -> FileResponse:
    """Thay đổi agent đã làm (job agent, hoặc job deploy có AI sửa cấu hình); áp bằng `git apply`."""
    job = _load_job(db, job_id)
    path = settings.patch_path(job.id)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("job.no_patch"))
    name = f"{job.service.project.name}-{job.service.name}-{job.kind}-{job.id}.patch"
    return FileResponse(path, media_type="text/x-diff", filename=name)


@router.get("/{job_id}/logs")
def get_job_logs(
    job_id: int,
    db: DB,
    queue: Queue,
    _user: CurrentUser,
    offset: int = Query(0, ge=0),
) -> JobLogChunk:
    """Đọc log tăng dần: frontend gửi lại next_offset để chỉ nhận phần mới."""
    job = _load_job(db, job_id)
    finished = job.status not in (JobStatus.QUEUED, JobStatus.RUNNING)
    path = queue.log_path(job_id)
    if not path.exists():
        return JobLogChunk(content="", next_offset=0, finished=finished)
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(MAX_LOG_CHUNK)
    # Chỉ cắt ở ranh giới dòng (trừ khi một dòng dài hơn cả chunk) để
    # không xẻ đôi ký tự UTF-8 tiếng Việt
    if len(data) == MAX_LOG_CHUNK and b"\n" in data:
        data = data[: data.rindex(b"\n") + 1]
    more_pending = len(data) == MAX_LOG_CHUNK or path.stat().st_size > offset + len(data)
    return JobLogChunk(
        content=data.decode("utf-8", errors="replace"),
        next_offset=offset + len(data),
        finished=finished and not more_pending,
    )
