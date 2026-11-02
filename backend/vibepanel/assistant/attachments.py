"""File người dùng đính kèm trong khung chat AI Agent (mã nguồn nén, bản dump database).

Tải lên trước khi gửi tin nhắn: file nằm ở data_dir/assistant/uploads/<token>/<tên>; gửi tin nhắn kèm
token thì file được chuyển vào data_dir/assistant/<phiên>/attachments/<tên> (xóa phiên là xóa luôn).
Agent gọi file bằng tên (create_service source=attachment, import_database...). Người dùng xóa được từng
file; phiên không hoạt động quá KEEP_DAYS ngày thì file đính kèm tự bị dọn (Manager.prune_attachments).
"""

import re
import secrets
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from ..config import Settings

MAX_UPLOAD = 5 * 1024 ** 3
# Mã nguồn nén và bản dump database (định dạng import database đọc được)
SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
            ".sql", ".sql.gz", ".dump", ".archive", ".archive.gz", ".json", ".jsonl", ".json.gz", ".jsonl.gz")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
STALE = 24 * 3600
KEEP_DAYS = 7


class AttachmentError(ValueError):
    pass


def allowed(name: str) -> bool:
    return name.lower().endswith(SUFFIXES)


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    base = re.sub(r"[^\w.\-]+", "_", base).strip("._") or "file"
    return base[:120]


def staging_dir(settings: Settings) -> Path:
    return settings.data_dir / "assistant" / "uploads"


def session_dir(settings: Settings, session_id: int) -> Path:
    return settings.data_dir / "assistant" / str(session_id) / "attachments"


def new_staging(settings: Settings, filename: str) -> tuple[str, Path]:
    """(token, đường dẫn file sẽ ghi). Dọn các bản tải lên bỏ dở quá một ngày."""
    if not allowed(filename):
        raise AttachmentError(filename)
    base = staging_dir(settings)
    if base.is_dir():
        for old in base.iterdir():
            if old.is_dir() and time.time() - old.stat().st_mtime > STALE:
                shutil.rmtree(old, ignore_errors=True)
    token = secrets.token_urlsafe(18)
    folder = base / token
    folder.mkdir(parents=True)
    return token, folder / safe_name(filename)


def claim(settings: Settings, session_id: int, tokens: list[str]) -> list[dict]:
    """Chuyển file đã tải lên vào phiên; trả [{name, size}] theo thứ tự. Token sai / hết hạn: bỏ qua."""
    out = []
    target = session_dir(settings, session_id)
    for token in tokens:
        if not TOKEN_RE.fullmatch(token):
            continue
        folder = staging_dir(settings) / token
        files = [p for p in folder.iterdir() if p.is_file()] if folder.is_dir() else []
        if not files:
            continue
        target.mkdir(parents=True, exist_ok=True)
        src = files[0]
        name = src.name
        stem, dot, ext = name.partition(".")
        n = 2
        while (target / name).exists():
            name = f"{stem}-{n}{dot}{ext}"
            n += 1
        size = src.stat().st_size
        src.rename(target / name)
        shutil.rmtree(folder, ignore_errors=True)
        out.append({"name": name, "size": size})
    return out


def path(settings: Settings, session_id: int, name: str) -> Path:
    folder = session_dir(settings, session_id)
    candidate = folder / safe_name(name or "")
    if not candidate.is_file():
        raise FileNotFoundError(name)
    return candidate


def listing(settings: Settings, session_id: int) -> list[Path]:
    folder = session_dir(settings, session_id)
    return sorted(p for p in folder.iterdir() if p.is_file()) if folder.is_dir() else []


def discard_staged(settings: Settings, token: str) -> None:
    """Bỏ file đã tải lên nhưng chưa gửi (người dùng gỡ khỏi ô soạn tin)."""
    if TOKEN_RE.fullmatch(token):
        shutil.rmtree(staging_dir(settings) / token, ignore_errors=True)


def remove(settings: Settings, session_id: int, name: str | None = None) -> None:
    """Xóa một file đính kèm của phiên, hoặc tất cả khi name là None."""
    if name is None:
        shutil.rmtree(session_dir(settings, session_id), ignore_errors=True)
        return
    path(settings, session_id, name).unlink()


def summary(settings: Settings, session_id: int) -> list[dict]:
    return [{"name": p.name, "size": p.stat().st_size} for p in listing(settings, session_id)]


def is_archive(file: Path) -> bool:
    if zipfile.is_zipfile(file):
        return True
    try:
        return tarfile.is_tarfile(file)
    except OSError:
        return False


def members(file: Path) -> list[str]:
    """Tên các file trong file nén (không giải nén)."""
    if zipfile.is_zipfile(file):
        with zipfile.ZipFile(file) as zf:
            return [i.filename for i in zf.infolist() if not i.is_dir()]
    with tarfile.open(file, "r:*") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def extract_member(file: Path, member: str, dest: Path) -> None:
    """Chép đúng một file trong file nén ra `dest` (chỉ đọc nội dung, không tạo đường dẫn theo file nén)."""
    if zipfile.is_zipfile(file):
        with zipfile.ZipFile(file) as zf, zf.open(member) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)
        return
    with tarfile.open(file, "r:*") as tf:
        info = tf.getmember(member)
        if not info.isfile():
            raise KeyError(member)
        with tf.extractfile(info) as src, dest.open("wb") as out:
            shutil.copyfileobj(src, out, 1024 * 1024)


def note(name: str, size: int) -> str:
    """Khối văn bản gửi cho model thay cho khối đính kèm."""
    return (f"[The user attached the file `{name}` ({size / 1024 / 1024:.1f} MB). Refer to it by this exact name in "
            "list_attachments, create_service (source=attachment) and import_database.]")
