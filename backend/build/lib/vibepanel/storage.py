"""Dung lượng ổ đĩa của từng service / database trong project, và tổng của project.

Mỗi mục gồm: lớp ghi của container, volume dữ liệu, image (service tự build tính cả các bản cũ
giữ để rollback), bản sao lưu (database) và mã nguồn + file riêng trên đĩa của panel.
Image dùng chung (vd hai database cùng postgres:17) chỉ tính một lần trong tổng của project.
`docker system df -v` chậm nên số liệu Docker được lưu đệm 60 giây.
"""

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import backups, compose, service_backups
from .config import Settings
from .docker import service_id_label
from .models import Project

CACHE_SECONDS = 60
_cache: dict = {"at": 0.0, "data": None}
_lock = threading.Lock()


@dataclass
class Item:
    kind: str  # service | database
    name: str
    containers: list[dict] = field(default_factory=list)  # {name, state, size}
    volumes: list[dict] = field(default_factory=list)  # {name, size}
    images: dict[str, int] = field(default_factory=dict)
    writable: int = 0
    data: int = 0
    image: int = 0
    backups: int = 0
    source: int = 0
    total: int = 0


def _docker_snapshot(docker, refresh: bool) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    with _lock:
        if refresh or _cache["data"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
            _cache["data"] = (docker.container_sizes(), docker.volume_sizes(), docker.image_sizes("vp/*"))
            _cache["at"] = time.monotonic()
        return _cache["data"]


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def project_storage(project: Project, docker, settings: Settings, refresh: bool = False) -> dict:
    containers, volumes, built = _docker_snapshot(docker, refresh)
    db_volumes = {d.volume for d in project.databases}
    items: list[Item] = []

    for service in sorted(project.services, key=lambda s: s.name):
        item = Item("service", service.name)
        for c in containers:
            if service_id_label(c["labels"]) == str(service.id):
                item.containers.append({"name": c["name"], "state": c["state"], "size": c["writable"]})
                item.writable += c["writable"]
                # Image kéo về (ứng dụng 1-click, image trong compose): kích thước lấy từ container
                if not c["image"].startswith("vp/"):
                    item.images[c["image"]] = c["image_size"]
        # Volume của service thường (vp_<slug>_<thư mục>) và của compose (vp_<slug>_<tên>)
        prefixes = (f"vp_{service.slug}_", f"{compose.project_name(service)}_")
        for name, size in volumes.items():
            if name.startswith(prefixes) and name not in db_volumes:
                item.volumes.append({"name": name, "size": size})
                item.data += size
        # Image tự build: vp/<slug>:<job> và vp/<slug>-<service compose>:<job>
        for ref, size in built.items():
            repo = ref.rsplit(":", 1)[0]
            if repo == f"vp/{service.slug}" or repo.startswith(f"vp/{service.slug}-"):
                item.images[ref] = size
        item.source = dir_size(settings.workspaces_dir / service.slug) + dir_size(settings.files_dir(service.id))
        item.backups = service_backups.total_size(settings, service)
        items.append(item)

    for database in sorted(project.databases, key=lambda d: d.name):
        item = Item("database", database.name)
        for c in containers:
            if c["name"] == database.container:
                item.containers.append({"name": c["name"], "state": c["state"], "size": c["writable"]})
                item.writable += c["writable"]
                item.images[c["image"]] = c["image_size"]
        if database.volume in volumes:
            item.volumes.append({"name": database.volume, "size": volumes[database.volume]})
            item.data = volumes[database.volume]
        item.backups = backups.total_size(settings, database)
        items.append(item)

    shared: dict[str, int] = {}
    for item in items:
        item.image = sum(item.images.values())
        item.total = item.writable + item.data + item.image + item.backups + item.source
        shared.update(item.images)
    total = sum(i.writable + i.data + i.backups + i.source for i in items) + sum(shared.values())
    return {
        "total": total,
        "images": sum(shared.values()),
        "measured_at": time.time() - (time.monotonic() - _cache["at"]),
        "items": [{k: v for k, v in vars(i).items() if k != "images"} | {"image_count": len(i.images)} for i in items],
    }
