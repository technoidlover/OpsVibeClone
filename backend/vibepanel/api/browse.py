"""File manager của service: xem / tải file của mã nguồn hoặc container đang chạy (xem filebrowser.py).

Container đang chạy sửa được mọi thứ: soạn / tạo file, tạo thư mục, đổi tên, xóa, tải file lên.
Mã nguồn: soạn / tạo / tải file lên, lưu thành file riêng để còn sau mỗi lần deploy (xem filebrowser.SourceEdits).
"""

import posixpath
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .. import archive, custom_files, filebrowser
from ..docker import DockerError
from ..i18n import t
from ..models import Job
from .deps import DB, AppSettings, DockerDep, ServiceDep

router = APIRouter(prefix="/api/projects", tags=["browse"])

Root = Literal["source", "live"]


class Entry(BaseModel):
    name: str
    type: str  # dir | file | link
    size: int
    mtime: int
    # Mã nguồn: file đã sửa / thêm trên panel (đang là file riêng)
    custom: bool = False


class Listing(BaseModel):
    # Nguồn xem được: source = mã nguồn lần deploy gần nhất, live = container đang chạy
    roots: list[str]
    root: str
    path: str
    parent: str | None
    container: str | None
    # Sửa được ở nguồn đang xem không (mã nguồn chạy từ volume website thì không)
    editable: bool = False
    entries: list[Entry]


class FileView(BaseModel):
    path: str
    size: int
    binary: bool
    truncated: bool
    content: str | None


class WriteFile(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    # Soạn trên panel: file văn bản, cùng giới hạn với xem trước (vài MB ký tự là đủ)
    content: str = Field(max_length=4 * filebrowser.PREVIEW_LIMIT)
    # true = tạo file mới (báo lỗi nếu đã có), false = ghi đè file đang sửa
    create: bool = False
    root: Root = "live"


class PathBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class RenameBody(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    to: str = Field(min_length=1, max_length=4096)


class Changed(BaseModel):
    path: str


def _roots(service, db, docker, settings) -> tuple[list[str], str | None]:
    roots = []
    if not service.image and (settings.workspaces_dir / service.slug).is_dir():
        roots.append("source")
    active = db.get(Job, service.active_job_id) if service.active_job_id else None
    container = active.container if active and active.container else None
    if container and docker is not None and docker.container_state(container) == "running":
        roots.append("live")
    else:
        container = None
    return roots, container


def _fs(root: str, service, db, docker, settings):
    roots, container = _roots(service, db, docker, settings)
    if root not in roots:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t(f"browse.no_{root}"))
    if root == "source":
        return filebrowser.SourceFS(settings.workspaces_dir / service.slug, service.name), roots, None
    workdir = (docker.inspect(container).get("Config") or {}).get("WorkingDir") or "/"
    return filebrowser.ContainerFS(docker, container, workdir), roots, container


def _errors(fn):
    try:
        return fn()
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("browse.not_found")) from None
    except FileExistsError:
        raise HTTPException(status.HTTP_409_CONFLICT, t("browse.exists")) from None
    except IsADirectoryError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("browse.not_file")) from None
    except (filebrowser.BrowseError, custom_files.FileError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from None
    except DockerError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from None


@router.get("/{project}/services/{service}/browse")
def browse(service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings,
           root: Root | None = None, path: str = Query("", max_length=4096)) -> Listing:
    roots, _ = _roots(service, db, docker, settings)
    if not roots:
        return Listing(roots=[], root="", path="", parent=None, container=None, entries=[])
    # Mặc định mở container đang chạy (xem được file thật và sửa được); image không liệt kê được
    # thư mục (thiếu sh / stat) thì quay về mã nguồn
    chosen = root or ("live" if "live" in roots else roots[0])
    fs, roots, container = _fs(chosen, service, db, docker, settings)
    current = fs.normalize(path)
    try:
        entries = _errors(lambda: fs.list(current))
    except HTTPException:
        if root or chosen != "live" or "source" not in roots:
            raise
        chosen = "source"
        fs, roots, container = _fs(chosen, service, db, docker, settings)
        current = fs.normalize(path)
        entries = _errors(lambda: fs.list(current))
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    editable = chosen == "live" or not service.site_volume
    if chosen == "source" and editable:
        custom = _source_edits(fs, service, settings).custom_paths()
        for e in entries:
            e["custom"] = e["type"] == "file" and (f"{current}/{e['name']}" if current else e["name"]) in custom
    # Mã nguồn: đường dẫn tương đối ("" = gốc); container: đường dẫn tuyệt đối
    at_top = current == ("/" if chosen == "live" else "")
    parent = None if at_top else posixpath.dirname(current)
    return Listing(roots=roots, root=chosen, path=current, parent=parent, container=container, editable=editable,
                   entries=[Entry(**e) for e in entries])


@router.get("/{project}/services/{service}/browse/file")
def view_file(service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings,
              root: Root, path: str = Query(..., min_length=1, max_length=4096)) -> FileView:
    fs, _, _ = _fs(root, service, db, docker, settings)
    data = _errors(lambda: fs.read(path) if root == "source" else fs.read(path, settings))
    return FileView(path=fs.normalize(path), **data)


@router.get("/{project}/services/{service}/browse/download")
def download(service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings,
             root: Root, path: str = Query("", max_length=4096)) -> FileResponse:
    """File lẻ tải nguyên bản; thư mục được nén .zip (tạo file tạm, xóa sau khi gửi xong)."""
    fs, _, _ = _fs(root, service, db, docker, settings)
    if root == "source":
        if _errors(lambda: fs.is_dir(path)):
            out = _errors(lambda: fs.zip(path, settings))
            name = f"{service.name}-{posixpath.basename(fs.normalize(path)) or 'source'}.zip"
            return FileResponse(out, filename=name, media_type="application/zip",
                                background=BackgroundTask(out.unlink, missing_ok=True))
        target = _errors(lambda: fs.file_path(path))
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")
    out, name = _errors(lambda: fs.download(path, settings))
    media = "application/zip" if name.endswith(".zip") else "application/octet-stream"
    return FileResponse(out, filename=name, media_type=media, background=BackgroundTask(out.unlink, missing_ok=True))


# ---------- Sửa file ----------

def _live(service, db, docker, settings) -> filebrowser.ContainerFS:
    return _fs("live", service, db, docker, settings)[0]


def _source_edits(fs, service, settings) -> filebrowser.SourceEdits:
    return filebrowser.SourceEdits(fs, settings.files_dir(service.id), service.root_dir, service.source == "manual")


def _source(service, db, docker, settings) -> filebrowser.SourceEdits:
    # Website PHP chạy từ volume: deploy dùng lại volume nên sửa mã nguồn không tới được web đang chạy
    if service.site_volume:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("browse.source_site_volume"))
    return _source_edits(_fs("source", service, db, docker, settings)[0], service, settings)


@router.put("/{project}/services/{service}/browse/file")
def write_file(req: WriteFile, service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings) -> Changed:
    if req.root == "source":
        edits = _source(service, db, docker, settings)
        return Changed(path=_errors(lambda: edits.write(req.path, req.content, create=req.create)))
    fs = _live(service, db, docker, settings)
    return Changed(path=_errors(lambda: fs.write(req.path, req.content.encode(), create=req.create)))


@router.post("/{project}/services/{service}/browse/mkdir")
def make_dir(req: PathBody, service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings) -> Changed:
    fs = _live(service, db, docker, settings)
    return Changed(path=_errors(lambda: fs.mkdir(req.path)))


@router.post("/{project}/services/{service}/browse/rename")
def rename(req: RenameBody, service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings) -> Changed:
    fs = _live(service, db, docker, settings)
    return Changed(path=_errors(lambda: fs.rename(req.path, req.to)))


@router.delete("/{project}/services/{service}/browse", status_code=status.HTTP_204_NO_CONTENT)
def delete(service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings,
           path: str = Query(..., min_length=1, max_length=4096), root: Root = "live") -> None:
    if root == "source":
        edits = _source(service, db, docker, settings)
        return _errors(lambda: edits.delete(path))
    fs = _live(service, db, docker, settings)
    _errors(lambda: fs.delete(path))


@router.post("/{project}/services/{service}/browse/upload")
async def upload(request: Request, service: ServiceDep, db: DB, docker: DockerDep, settings: AppSettings,
                 path: str = Query(..., min_length=1, max_length=4096), overwrite: bool = False,
                 root: Root = "live") -> Changed:
    """Thân request là nội dung file (application/octet-stream): lưu tạm trên đĩa panel rồi đẩy vào container
    (hoặc lưu thành file riêng với mã nguồn)."""
    pick = _source if root == "source" else _live
    fs = await run_in_threadpool(pick, service, db, docker, settings)
    too_large = HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                              t("archive.upload_too_large", limit=archive.MAX_UPLOAD // archive.GB))
    if int(request.headers.get("content-length") or 0) > archive.MAX_UPLOAD:
        raise too_large
    temp = filebrowser.tmp_dir(settings) / f"upload-{time.time_ns()}"
    try:
        size = 0
        with temp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > archive.MAX_UPLOAD:
                    raise too_large
                out.write(chunk)

        def push():
            if root == "source":
                return fs.write(path, temp, create=not overwrite)
            with temp.open("rb") as data:
                return fs.write(path, data, create=not overwrite)

        return Changed(path=await run_in_threadpool(_errors, push))
    finally:
        temp.unlink(missing_ok=True)
