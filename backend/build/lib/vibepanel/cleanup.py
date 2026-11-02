"""Dọn tài nguyên Docker không dùng sau khi xóa project / service / database.

Theo yêu cầu: xóa xong thì VPS không còn rác. Chạy nền sau mỗi lần xóa:
- container đã dừng / lỗi (mọi container trên máy);
- image không container nào dùng, trừ image cần giữ: image rollback của service còn
  tồn tại, Traefik, panel, image nền Nixpacks (xóa thì build sau phải tải lại);
- volume ẩn danh không dùng và volume do panel tạo (vp_*) không còn thuộc service /
  database nào. Volume của ứng dụng chạy ngoài panel được giữ nguyên; volume người dùng
  chọn "giữ lại dữ liệu" khi xóa cũng vậy (ghi trong settings).
- build cache cũ hơn 7 ngày.
"""

import json
import logging
import re
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from . import panel_settings, templates
from .config import Settings
from .docker import Docker, DockerError
from .models import Database, Job, JobStatus, Service

logger = logging.getLogger(__name__)

KEPT_VOLUMES = "kept_volumes"
_ANONYMOUS = re.compile(r"^[0-9a-f]{64}$")
_lock = threading.Lock()
WAIT_SECONDS = 15
WAIT_ROUNDS = 240


def keep_volumes(db: Session, names: list[str]) -> None:
    """Ghi nhớ volume người dùng chọn giữ lại khi xóa, để lần dọn sau không xóa mất."""
    kept = set(json.loads(panel_settings.get(db, KEPT_VOLUMES) or "[]")) | set(names)
    panel_settings.put(db, KEPT_VOLUMES, json.dumps(sorted(kept)))


def owned_volumes(db: Session) -> set[str]:
    names = {d.volume for d in db.scalars(select(Database))}
    for service in db.scalars(select(Service)):
        names |= {templates.volume_name(service.slug, m) for m in service.volumes}
        if service.site_volume:
            names.add(service.site_volume)
    return names | set(json.loads(panel_settings.get(db, KEPT_VOLUMES) or "[]"))


def _protected_image(ref: str, keep: set[str], panel_repo: str | None, traefik: str) -> bool:
    repo = ref.rsplit(":", 1)[0]
    return (ref in keep or ref == traefik or repo == panel_repo
            or repo.startswith("ghcr.io/railwayapp/nixpacks"))


def run(docker: Docker, sessions: sessionmaker[Session], settings: Settings) -> list[str]:
    """Dọn một lượt; trả về tóm tắt để ghi log. Không chạy chồng hai lượt."""
    with _lock:
        summary: list[str] = []
        try:
            stopped = docker.stopped_containers()
            for name in stopped:
                docker.remove_container(name)
            summary.append(f"container đã dừng: {len(stopped)}")

            with sessions() as db:
                keep = {img for img in db.scalars(select(Job.image).where(Job.image.is_not(None)))}
                volumes = owned_volumes(db)
            panel_image = docker.container_image(settings.panel_container)
            panel_repo = panel_image.rsplit(":", 1)[0] if panel_image else None
            used = docker.used_image_ids()
            removed = 0
            for image_id, refs in docker.list_images().items():
                if image_id in used or any(_protected_image(r, keep, panel_repo, settings.traefik_image) for r in refs):
                    continue
                for ref in refs or [image_id]:
                    removed += docker.remove_image_force(ref)
            summary.append(f"image: {removed}")

            gone = [v for v in docker.unused_volumes()
                    if _ANONYMOUS.match(v) or (v.startswith("vp_") and v not in volumes)]
            for volume in gone:
                docker.remove_volume(volume)
            summary.append(f"volume: {len(gone)}")
            summary += docker.prune("168h")
        except DockerError as e:
            summary.append(f"lỗi: {e}")
        for line in summary:
            logger.info("Dọn dẹp sau khi xóa: %s", line)
        return summary


def _busy(sessions: sessionmaker[Session]) -> bool:
    with sessions() as db:
        return db.scalar(select(Job.id).where(Job.status == JobStatus.RUNNING).limit(1)) is not None


def _run_when_idle(docker: Docker, sessions: sessionmaker[Session], settings: Settings) -> None:
    # Đang có deploy: container cũ có thể đang tạm dừng chờ khôi phục, image mới chưa chạy.
    # Chờ deploy xong (tối đa 1 giờ) rồi mới dọn.
    for _ in range(WAIT_ROUNDS):
        if not _busy(sessions):
            break
        time.sleep(WAIT_SECONDS)
    run(docker, sessions, settings)


def run_in_background(docker: Docker | None, sessions: sessionmaker[Session], settings: Settings) -> None:
    if docker is not None:
        threading.Thread(target=_run_when_idle, args=(docker, sessions, settings), name="opsvibes-cleanup",
                         daemon=True).start()
