"""Deploy từ file nén: giải nén an toàn file .zip / .tar(.gz|.bz2|.xz) người dùng tải lên.

File nén là dữ liệu không tin cậy nên mọi đường dẫn đều được kiểm tra trước khi
ghi: không cho thoát khỏi thư mục đích (zip slip, đường dẫn tuyệt đối, symlink trỏ
ra ngoài), không tạo file thiết bị, giới hạn số file và tổng dung lượng sau giải
nén (chống zip bomb). RAR / 7z không hỗ trợ vì cần cài thêm công cụ ngoài.

Ngoài giới hạn cứng, mọi bước ghi lớn (tải lên, giải nén) đều kiểm tra dung lượng
trống thực tế của ổ đĩa và luôn chừa lại RESERVE để server không bị đầy.
"""

import os
import shutil
import tarfile
import zipfile
from pathlib import Path

from .i18n import t

GB = 1024 ** 3
MAX_UPLOAD = 5 * GB
MAX_EXTRACTED = 20 * GB
# Dung lượng luôn chừa lại trên ổ đĩa sau khi ghi
RESERVE = 1 * GB
MAX_ENTRIES = 100_000
# Rác của macOS khi nén bằng Finder
JUNK = {"__MACOSX", ".DS_Store"}


class ArchiveError(ValueError):
    pass


def detect(path: Path) -> str | None:
    """"zip" | "tar" | None (không phải định dạng hỗ trợ)."""
    if zipfile.is_zipfile(path):
        return "zip"
    try:
        if tarfile.is_tarfile(path):
            return "tar"
    except OSError:
        pass
    return None


def _target(root: Path, name: str) -> Path:
    if not name or name.startswith(("/", "\\")) or "\x00" in name:
        raise ArchiveError(t("archive.unsafe_path", name=name))
    target = (root / name).resolve()
    if target != root and not target.is_relative_to(root):
        raise ArchiveError(t("archive.unsafe_path", name=name))
    return target


def gb(size: int) -> str:
    return f"{size / GB:.1f}"


def free_space(path: Path) -> int:
    """Dung lượng còn ghi được (đã trừ RESERVE) trên ổ chứa `path`."""
    while not path.exists():
        path = path.parent
    return shutil.disk_usage(path).free - RESERVE


def check_space(path: Path, needed: int) -> None:
    free = free_space(path)
    if needed > free:
        raise ArchiveError(t("archive.no_space", need=gb(needed), free=gb(max(free, 0)), reserve=gb(RESERVE)))


def _check_totals(dest: Path, count: int, size: int) -> None:
    if count > MAX_ENTRIES:
        raise ArchiveError(t("archive.too_many_files", limit=MAX_ENTRIES))
    if size > MAX_EXTRACTED:
        raise ArchiveError(t("archive.too_large", limit=MAX_EXTRACTED // GB))
    # File giải nén + bản sao trong git cục bộ (xem pipelines.extract_source)
    check_space(dest, 2 * size)


def _extract_zip(path: Path, dest: Path) -> None:
    with zipfile.ZipFile(path) as zf:
        members = zf.infolist()
        _check_totals(dest, len(members), sum(m.file_size for m in members))
        for member in members:
            _target(dest, member.filename)
        # zipfile không tạo symlink; đường dẫn đã được kiểm tra ở trên
        zf.extractall(dest)


def _extract_tar(path: Path, dest: Path) -> None:
    with tarfile.open(path, "r:*") as tf:
        members = tf.getmembers()
        _check_totals(dest, len(members), sum(m.size for m in members if m.isfile()))
        for member in members:
            target = _target(dest, member.name)
            if member.ischr() or member.isblk() or member.isfifo() or member.isdev():
                raise ArchiveError(t("archive.unsafe_path", name=member.name))
            if member.issym() or member.islnk():
                link = member.linkname
                base = target.parent if member.issym() else dest
                if link.startswith("/") or not (base / link).resolve().is_relative_to(dest):
                    raise ArchiveError(t("archive.unsafe_link", name=member.name))
        if hasattr(tarfile, "data_filter"):
            # Python 3.12 (image panel): bộ lọc chuẩn còn bỏ quyền setuid và chủ sở hữu
            tf.extractall(dest, filter="data")
        else:
            for member in members:
                member.mode &= 0o755
            tf.extractall(dest)


def _flatten(dest: Path) -> str | None:
    """Nén cả thư mục cha (repo-main.zip chứa repo-main/): đưa nội dung ra gốc. Trả tên thư mục đã gỡ."""
    for name in JUNK:
        junk = dest / name
        if junk.is_dir():
            shutil.rmtree(junk, ignore_errors=True)
        elif junk.exists():
            junk.unlink()
    entries = list(dest.iterdir())
    if len(entries) != 1 or not entries[0].is_dir() or entries[0].is_symlink():
        return None
    inner = entries[0]
    temp = dest / f".vp-flatten-{os.getpid()}"
    inner.rename(temp)
    for item in temp.iterdir():
        item.rename(dest / item.name)
    temp.rmdir()
    return inner.name


def extract(path: Path, dest: Path) -> str | None:
    """Giải nén vào `dest` (phải chưa tồn tại). Trả tên thư mục gốc đã được làm phẳng, nếu có."""
    kind = detect(path)
    if kind is None:
        raise ArchiveError(t("archive.unsupported"))
    dest.mkdir(parents=True)
    root = dest.resolve()
    try:
        (_extract_zip if kind == "zip" else _extract_tar)(path, root)
        flattened = _flatten(root)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError) as e:
        raise ArchiveError(t("archive.corrupt", error=str(e))) from e
    if not any(root.iterdir()):
        raise ArchiveError(t("archive.empty"))
    return flattened
