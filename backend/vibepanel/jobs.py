"""Hàng đợi job build/deploy.

Thay cho BackgroundTasks của bản cũ:
- Giới hạn số build song song (OPSVIBES_MAX_CONCURRENT_BUILDS).
- Mỗi service chỉ chạy một job tại một thời điểm.
- Job mới thay thế các job còn đang chờ của cùng service (push liên tục chỉ build bản cuối).
- Trạng thái lưu trong DB; khởi động lại panel sẽ chạy tiếp job đang chờ.
"""

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .crypto import SecretBox
from .docker import Docker
from .i18n import t
from .models import Job, JobKind, JobStatus, JobTrigger, Service, utcnow
from .runner import JobLogger

logger = logging.getLogger(__name__)

Pipeline = Callable[["JobContext"], None]

# Job deploy/rollback mới thay thế job deploy/rollback đang chờ; job agent thì không
_REPLACEABLE = (JobKind.DEPLOY, JobKind.ROLLBACK)


class JobContext:
    def __init__(
        self,
        job: Job,
        service: Service,
        log: JobLogger,
        settings: Settings,
        box: SecretBox,
        db: Session,
        docker: Docker | None,
    ):
        self.job = job
        self.service = service
        self.log = log
        self.settings = settings
        self.box = box
        self.db = db
        self._docker = docker
        # Volume mã nguồn (site_files.py) do deploy quyết định: None (rollback: dùng volume đang có),
        # "off" (không dùng), "reuse" (giữ thay đổi trên web), "fresh" (dựng lại từ file nén mới)
        self.site_mode: str | None = None

    @property
    def docker(self) -> Docker:
        if self._docker is None:
            raise RuntimeError(t("jobs.no_docker"))
        return self._docker

    @property
    def workspace(self) -> Path:
        return self.settings.workspaces_dir / self.service.slug

    @property
    def build_dir(self) -> Path:
        """Thư mục build: thư mục con root_dir của repo (monorepo), không thì cả workspace."""
        return self.workspace / self.service.root_dir if self.service.root_dir else self.workspace


class JobQueue:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        box: SecretBox,
        pipelines: dict[str, Pipeline],
        docker: Docker | None = None,
    ):
        self.settings = settings
        self.sessions = sessions
        self.box = box
        self.pipelines = pipelines
        self.docker = docker
        self._executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_builds, thread_name_prefix="opsvibes-job"
        )
        self._service_locks: defaultdict[int, threading.Lock] = defaultdict(threading.Lock)

    def log_path(self, job_id: int) -> Path:
        return self.settings.job_logs_dir / f"{job_id}.log"

    def repair_reused_logs(self) -> None:
        """Một lần: tách log bị ghi nối do ID job bị dùng lại (bản trước 0.17.1), chỉ giữ lần chạy cuối."""
        marker = self.settings.job_logs_dir / ".reused-ids-repaired"
        if marker.exists():
            return
        ends = ("==> Hoàn tất", "==> Done", "==> Thất bại", "==> Failed")
        for path in self.settings.job_logs_dir.glob("*.log"):
            lines = path.read_text(errors="replace").splitlines(keepends=True)
            finished = [i for i, line in enumerate(lines) if line.startswith(ends)]
            if len(finished) > 1:
                path.write_text("".join(lines[finished[-2] + 1:]))
                logger.info("Tách log job bị ghi nối: %s", path.name)
        marker.touch()

    def recover(self) -> None:
        """Gọi lúc khởi động: job đang chạy dở coi như thất bại, job đang chờ được chạy lại."""
        self.repair_reused_logs()
        with self.sessions() as db:
            db.execute(
                update(Job)
                .where(Job.status == JobStatus.RUNNING)
                .values(status=JobStatus.FAILED, error=t("jobs.interrupted"), finished_at=utcnow())
            )
            db.commit()
            queued = db.scalars(select(Job.id).where(Job.status == JobStatus.QUEUED).order_by(Job.id)).all()
        for job_id in queued:
            self._executor.submit(self._run, job_id)

    def enqueue(
        self,
        db: Session,
        service: Service,
        kind: str,
        trigger: JobTrigger,
        branch: str | None = None,
        source: Job | None = None,
        agent: str | None = None,
        prompt: str | None = None,
        patch: Job | None = None,
        allow_code: bool = False,
        prepare: bool = False,
    ) -> Job:
        if kind not in self.pipelines:
            raise ValueError(t("jobs.unsupported_kind", kind=kind))
        if kind in _REPLACEABLE:
            db.execute(
                update(Job)
                .where(Job.service_id == service.id, Job.status == JobStatus.QUEUED, Job.kind.in_(_REPLACEABLE))
                .values(status=JobStatus.CANCELLED, error=t("jobs.superseded"), finished_at=utcnow())
            )
        job = Job(
            service_id=service.id,
            kind=kind,
            trigger=trigger,
            branch=source.branch if source else (branch or service.branch),
            commit_sha=source.commit_sha if source else None,
            source_job_id=source.id if source else None,
            agent=agent,
            prompt=prompt,
            patch_job_id=patch.id if patch else None,
            # Mọi lần deploy (thủ công hay webhook) dùng lựa chọn AI đã lưu của service
            heal_agent=service.deploy_agent if kind == JobKind.DEPLOY else None,
            # Chuẩn bị bằng AI khi được yêu cầu, hoặc tự động khi service chưa từng chạy được
            ai_prepare=bool(kind == JobKind.DEPLOY and service.deploy_agent and not service.image
                            and (prepare or service.active_job_id is None)),
            allow_code=allow_code,
        )
        db.add(job)
        db.commit()
        # SQLite cấp lại ID của job đã xóa (khi xóa service): bỏ log/patch cũ còn sót, không thì
        # log lần deploy này bị ghi nối vào log của web khác
        self.remove_files(job.id)
        self._executor.submit(self._run, job.id)
        return job

    def remove_files(self, *job_ids: int) -> None:
        for job_id in job_ids:
            self.log_path(job_id).unlink(missing_ok=True)
            self.settings.patch_path(job_id).unlink(missing_ok=True)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run(self, job_id: int) -> None:
        with self.sessions() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            lock = self._service_locks[job.service_id]

        with lock, self.sessions() as db:
            job = db.get(Job, job_id)
            # Có thể đã bị hủy trong lúc chờ khóa
            if job is None or job.status != JobStatus.QUEUED:
                return
            job.status = JobStatus.RUNNING
            job.started_at = utcnow()
            db.commit()

            log = JobLogger(self.log_path(job_id))
            try:
                ctx = JobContext(job, job.service, log, self.settings, self.box, db, self.docker)
                self.pipelines[job.kind](ctx)
                job.status = JobStatus.SUCCESS
                log.write(t("jobs.done"))
            except Exception as e:  # noqa: BLE001 - mọi lỗi đều phải ghi nhận vào job
                logger.exception("Job %s thất bại", job_id)
                job.status = JobStatus.FAILED
                job.error = log.redact(str(e))
                log.write(t("jobs.failed", error=e))
            finally:
                job.finished_at = utcnow()
                db.commit()
                log.close()
