"""File riêng của service: Dockerfile, compose.yml, file .sql, config... người dùng tự soạn hoặc tải lên.

Lưu ở data_dir/files/<service_id>/. Mỗi lần deploy, panel chép chúng vào thư mục build
(ghi đè file cùng tên trong repo / file nén). Service nguồn "manual" chỉ gồm các file này.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .i18n import t

# Tệp soạn ngay trên panel (không phải file tải lên) tối đa 1 MB
MAX_TEXT = 1024 ** 2
MAX_FILES = 200
_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
COMPOSE_NAMES = ("compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml")


class FileError(ValueError):
    pass


@dataclass
class FileInfo:
    path: str
    size: int
    text: bool


def validate_path(value: str) -> str:
    value = value.strip()
    parts = value.split("/")
    if (not value or len(value) > 255 or not _PATH_RE.fullmatch(value)
            or any(p in (".", "..") for p in parts) or parts[0] == ".git"):
        raise FileError(t("files.bad_path"))
    return value


def is_text(path: Path) -> bool:
    if path.stat().st_size > MAX_TEXT:
        return False
    try:
        path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def list_files(root: Path) -> list[FileInfo]:
    if not root.is_dir():
        return []
    return [FileInfo(p.relative_to(root).as_posix(), p.stat().st_size, is_text(p))
            for p in sorted(root.rglob("*")) if p.is_file() and not _is_partial(p)]


def _is_partial(path: Path) -> bool:
    # File đang tải lên dở (api/files.py ghi vào ".<tên>.<thời gian>.upload" rồi mới đổi tên)
    return path.name.startswith(".") and path.name.endswith(".upload")


def _target(root: Path, path: str) -> Path:
    return root / validate_path(path)


def read_text(root: Path, path: str) -> str:
    target = _target(root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    if not is_text(target):
        raise FileError(t("files.not_text"))
    return target.read_text()


def _check_room(root: Path, target: Path) -> None:
    if not target.exists() and len(list_files(root)) >= MAX_FILES:
        raise FileError(t("files.too_many", max=MAX_FILES))


def write_text(root: Path, path: str, content: str) -> None:
    if len(content.encode()) > MAX_TEXT:
        raise FileError(t("files.text_too_large"))
    target = _target(root, path)
    _check_room(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)


def upload_target(root: Path, path: str) -> Path:
    """Đường dẫn đích cho file tải lên (đã kiểm tra tên và số lượng)."""
    target = _target(root, path)
    _check_room(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def delete(root: Path, path: str) -> None:
    target = _target(root, path)
    if not target.is_file():
        raise FileNotFoundError(path)
    target.unlink()
    # Dọn thư mục con rỗng
    parent = target.parent
    while parent != root and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def apply(root: Path, dest: Path) -> list[str]:
    """Chép file riêng vào thư mục build, ghi đè file cùng tên. Trả danh sách đã chép."""
    copied = []
    for info in list_files(root):
        target = dest / info.path
        if target.is_dir() or target.is_symlink():
            shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / info.path, target)
        copied.append(info.path)
    return copied


def sync_back(workspace: Path, root: Path) -> list[str]:
    """Service tự viết: mã nguồn chính là file riêng, nên file AI Agent sửa / thêm được ghi lại vào đây."""
    changed = []
    for path in sorted(workspace.rglob("*")):
        rel = path.relative_to(workspace)
        if not path.is_file() or rel.parts[0] == ".git":
            continue
        target = root / rel
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            changed.append(rel.as_posix())
    return changed


def compose_file(build_dir: Path) -> Path | None:
    return next((build_dir / n for n in COMPOSE_NAMES if (build_dir / n).is_file()), None)
