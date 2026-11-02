"""File riêng của service: soạn Dockerfile / compose.yml, tải lên .sql, config... (xem custom_files.py)."""

import time

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .. import archive, custom_files
from ..i18n import t
from .deps import AppSettings, ServiceDep

router = APIRouter(prefix="/api/projects", tags=["files"])

PathParam = Query(min_length=1, max_length=255)


class FileOut(BaseModel):
    path: str
    size: int
    # Soạn được trên panel (UTF-8, không quá 1 MB)
    text: bool


class FileContent(BaseModel):
    path: str = Field(min_length=1, max_length=255)
    content: str


def _root(service, settings):
    if service.image:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, t("files.image_service"))
    return settings.files_dir(service.id)


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


@router.get("/{project}/services/{service}/files")
def list_files(service: ServiceDep, settings: AppSettings) -> list[FileOut]:
    return [FileOut(**vars(f)) for f in custom_files.list_files(_root(service, settings))]


@router.get("/{project}/services/{service}/files/content")
def read_file(service: ServiceDep, settings: AppSettings, path: str = PathParam) -> FileContent:
    try:
        return FileContent(path=path, content=custom_files.read_text(_root(service, settings), path))
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("files.not_found")) from None
    except custom_files.FileError as e:
        raise _bad(e) from None


@router.put("/{project}/services/{service}/files/content")
def write_file(req: FileContent, service: ServiceDep, settings: AppSettings) -> FileOut:
    root = _root(service, settings)
    try:
        custom_files.write_text(root, req.path, req.content)
        path = custom_files.validate_path(req.path)
    except custom_files.FileError as e:
        raise _bad(e) from None
    return FileOut(path=path, size=(root / path).stat().st_size, text=True)


@router.post("/{project}/services/{service}/files/upload")
async def upload_file(request: Request, service: ServiceDep, settings: AppSettings, path: str = PathParam) -> FileOut:
    """Thân request là nội dung file (application/octet-stream), như upload file nén."""
    root = _root(service, settings)
    try:
        target = custom_files.upload_target(root, path)
    except custom_files.FileError as e:
        raise _bad(e) from None
    too_large = HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                              t("archive.upload_too_large", limit=archive.MAX_UPLOAD // archive.GB))
    expected = int(request.headers.get("content-length") or 0)
    if expected > archive.MAX_UPLOAD:
        raise too_large
    try:
        archive.check_space(root, expected)
    except archive.ArchiveError as e:
        raise HTTPException(status.HTTP_507_INSUFFICIENT_STORAGE, str(e)) from None
    temp = target.with_name(f".{target.name}.{time.time_ns()}.upload")
    size = 0
    try:
        with temp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > archive.MAX_UPLOAD:
                    raise too_large
                out.write(chunk)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)
    return FileOut(path=target.relative_to(root).as_posix(), size=size, text=custom_files.is_text(target))


@router.delete("/{project}/services/{service}/files", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(service: ServiceDep, settings: AppSettings, path: str = PathParam) -> None:
    try:
        custom_files.delete(_root(service, settings), path)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, t("files.not_found")) from None
    except custom_files.FileError as e:
        raise _bad(e) from None
