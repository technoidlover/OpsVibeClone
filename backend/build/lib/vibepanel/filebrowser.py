"""Duyệt file của service: mã nguồn lần deploy gần nhất (workspace trên đĩa panel) hoặc container đang chạy.

Container cho thấy file thật lúc chạy (ảnh upload, plugin WordPress cài thêm, file trong volume),
hữu ích với web không deploy từ GitHub. Tải về: file lẻ giữ nguyên, thư mục được nén .zip.
"""

import os
import posixpath
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from . import custom_files
from .i18n import t

# Xem trước nội dung file tối đa 1 MB
PREVIEW_LIMIT = 1024 ** 2
COPY_TIMEOUT = 3600
# ZIP không lưu được thời gian trước 1980 (image build kiểu reproducible đặt mtime = 1970)
ZIP_EPOCH = 315532800
# Thư mục git cục bộ panel tạo trong workspace: không phải mã nguồn của người dùng
HIDDEN = {".git"}


class BrowseError(ValueError):
    pass


def tmp_dir(settings) -> Path:
    path = settings.data_dir / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _temp(settings, suffix: str) -> Path:
    return tmp_dir(settings) / f"browse-{os.getpid()}-{time.time_ns()}{suffix}"


def _decode(data: bytes, size: int) -> dict:
    truncated = size > PREVIEW_LIMIT
    sample = data[:PREVIEW_LIMIT]
    if b"\0" in sample[:8192]:
        return {"size": size, "binary": True, "truncated": truncated, "content": None}
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError as e:
        if truncated and e.start >= len(sample) - 4:
            text = sample[:e.start].decode("utf-8")  # cắt ngang một ký tự nhiều byte ở cuối
        else:
            return {"size": size, "binary": True, "truncated": truncated, "content": None}
    return {"size": size, "binary": False, "truncated": truncated, "content": text}


# ---------- Mã nguồn (workspace) ----------

class SourceFS:
    def __init__(self, root: Path, name: str = "source"):
        # name: tên thư mục gốc khi nén cả mã nguồn (tên service)
        self.root, self.name = root.resolve(), name

    def target(self, path: str) -> Path:
        """Đường dẫn trong mã nguồn (có thể chưa tồn tại), không cho ra ngoài thư mục mã nguồn."""
        rel = path.strip().strip("/")
        if any(part in ("..",) for part in rel.split("/")) or rel.split("/")[0] in HIDDEN:
            raise BrowseError(t("browse.bad_path"))
        target = (self.root / rel) if rel else self.root
        # Không đi theo symlink ra ngoài thư mục mã nguồn
        if not target.resolve().is_relative_to(self.root):
            raise BrowseError(t("browse.bad_path"))
        return target

    def _path(self, path: str) -> Path:
        target = self.target(path)
        if not target.exists():
            raise FileNotFoundError(path)
        return target

    def normalize(self, path: str) -> str:
        return path.strip().strip("/")

    def list(self, path: str) -> list[dict]:
        target = self._path(path)
        if not target.is_dir():
            raise BrowseError(t("browse.not_dir"))
        out = []
        for p in target.iterdir():
            if target == self.root and p.name in HIDDEN:
                continue
            st = p.lstat()
            kind = "link" if p.is_symlink() else "dir" if p.is_dir() else "file"
            out.append({"name": p.name, "type": kind, "size": st.st_size if kind == "file" else 0,
                        "mtime": int(st.st_mtime)})
        return out

    def is_dir(self, path: str) -> bool:
        return self._path(path).is_dir()

    def read(self, path: str) -> dict:
        target = self._path(path)
        if not target.is_file():
            raise BrowseError(t("browse.not_file"))
        with target.open("rb") as f:
            data = f.read(PREVIEW_LIMIT + 1)
        return _decode(data, target.stat().st_size)

    def file_path(self, path: str) -> Path:
        target = self._path(path)
        if not target.is_file():
            raise BrowseError(t("browse.not_file"))
        return target

    def zip(self, path: str, settings) -> Path:
        target = self._path(path)
        out = _temp(settings, ".zip")
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True, strict_timestamps=False) as zf:
            base = self.name if target == self.root else target.name
            for p in sorted(target.rglob("*")):
                rel = p.relative_to(target)
                if target == self.root and rel.parts and rel.parts[0] in HIDDEN:
                    continue
                if p.is_symlink() or not (p.is_file() or p.is_dir()):
                    continue
                arc = f"{base}/{rel.as_posix()}"
                if p.is_dir():
                    zf.writestr(arc + "/", b"")
                else:
                    zf.write(p, arc)
        return out


class SourceEdits:
    """Sửa mã nguồn: lưu thành file riêng (custom_files) để mọi lần deploy sau vẫn chép đè lên repo / file nén,
    đồng thời ghi vào workspace để xem được ngay. File riêng tính từ thư mục app (root_dir) của service.
    """

    def __init__(self, fs: SourceFS, files_root: Path, root_dir: str | None, manual: bool):
        self.fs, self.files_root, self.manual = fs, files_root, manual
        self.base = (root_dir or "").strip().strip("/")

    def _rel(self, path: str) -> tuple[Path, str]:
        target = self.fs.target(path)
        rel = target.relative_to(self.fs.root).as_posix() if target != self.fs.root else ""
        if not rel:
            raise BrowseError(t("browse.bad_path"))
        return target, rel

    def _custom(self, rel: str) -> str:
        if self.base:
            if not rel.startswith(self.base + "/"):
                raise BrowseError(t("browse.outside_app", dir=self.base))
            rel = rel[len(self.base) + 1:]
        return custom_files.validate_path(rel)

    def custom_paths(self) -> set[str]:
        """Đường dẫn (tính từ gốc mã nguồn) của các file đã sửa / thêm, tức đang là file riêng."""
        prefix = f"{self.base}/" if self.base else ""
        return {prefix + f.path for f in custom_files.list_files(self.files_root)}

    def write(self, path: str, data: str | Path, create: bool = False) -> str:
        """`data`: nội dung soạn trên panel, hoặc file tạm đã tải lên."""
        target, rel = self._rel(path)
        if target.is_dir() and not target.is_symlink():
            raise IsADirectoryError(rel)
        if create and (target.exists() or target.is_symlink()):
            raise FileExistsError(rel)
        name = self._custom(rel)
        if isinstance(data, str):
            custom_files.write_text(self.files_root, name, data)
        else:
            shutil.copyfile(data, custom_files.upload_target(self.files_root, name))
        if target.is_symlink():
            target.unlink()
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.files_root / name, target)
        return rel

    def delete(self, path: str) -> None:
        """Chỉ xóa được file đã sửa / thêm: bỏ file riêng, lần deploy sau dùng lại bản trong repo / file nén."""
        target, rel = self._rel(path)
        name = self._custom(rel)
        if not (self.files_root / name).is_file():
            raise BrowseError(t("browse.source_delete_repo"))
        custom_files.delete(self.files_root, name)
        # Service tự viết: mã nguồn chính là file riêng, nên xóa luôn khỏi workspace
        if self.manual:
            target.unlink(missing_ok=True)


# ---------- Container đang chạy ----------

class ContainerFS:
    def __init__(self, docker, container: str, workdir: str):
        self.docker, self.container, self.workdir = docker, container, workdir or "/"

    def normalize(self, path: str) -> str:
        path = path.strip() or self.workdir
        if not path.startswith("/"):
            path = posixpath.join(self.workdir, path)
        return posixpath.normpath(path)

    def list(self, path: str) -> list[dict]:
        return self.docker.list_dir(self.container, self.normalize(path))

    def _copy(self, path: str, settings) -> Path:
        tar = _temp(settings, ".tar")
        try:
            self.docker.copy_out(self.container, self.normalize(path), tar, COPY_TIMEOUT)
        except Exception:
            tar.unlink(missing_ok=True)
            raise
        return tar

    def read(self, path: str, settings) -> dict:
        tar = self._copy(path, settings)
        try:
            with tarfile.open(tar) as tf:
                member = tf.next()
                if member is None or not member.isfile():
                    raise BrowseError(t("browse.not_file"))
                f = tf.extractfile(member)
                return _decode(f.read(PREVIEW_LIMIT + 1) if f else b"", member.size)
        finally:
            tar.unlink(missing_ok=True)

    def _target(self, path: str) -> str:
        """Đường dẫn được sửa / xóa: không cho đụng vào chính thư mục gốc /."""
        target = self.normalize(path)
        if target == "/" or not path.strip():
            raise BrowseError(t("browse.bad_path"))
        return target

    def write(self, path: str, data, create: bool = False) -> str:
        """Ghi nội dung (bytes hoặc file mở sẵn); `create` = file mới, báo lỗi nếu đã có."""
        target = self._target(path)
        self.docker.fs_change(self.container, "create" if create else "write", [target], data, COPY_TIMEOUT)
        return target

    def mkdir(self, path: str) -> str:
        target = self._target(path)
        self.docker.fs_change(self.container, "mkdir", [target])
        return target

    def rename(self, path: str, to: str) -> str:
        source, dest = self._target(path), self._target(to)
        self.docker.fs_change(self.container, "rename", [source, dest])
        return dest

    def delete(self, path: str) -> None:
        self.docker.fs_change(self.container, "delete", [self._target(path)], timeout=COPY_TIMEOUT)

    def download(self, path: str, settings) -> tuple[Path, str]:
        """(file tạm, tên tải về): file lẻ giữ nguyên, thư mục chuyển từ tar sang zip."""
        tar = self._copy(path, settings)
        try:
            with tarfile.open(tar) as tf:
                first = tf.next()
                if first is not None and first.isfile():
                    out = _temp(settings, "")
                    with tf.extractfile(first) as src, out.open("wb") as dst:
                        while chunk := src.read(1024 * 1024):
                            dst.write(chunk)
                    return out, posixpath.basename(first.name)
            out = _temp(settings, ".zip")
            with tarfile.open(tar) as tf, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for member in tf:
                    name = member.name.lstrip("/")
                    if member.isdir():
                        zf.writestr(name.rstrip("/") + "/", b"")
                    elif member.isfile():
                        info = zipfile.ZipInfo(name, time.localtime(max(member.mtime, ZIP_EPOCH))[:6])
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = (member.mode & 0o777) << 16
                        with tf.extractfile(member) as src, zf.open(info, "w", force_zip64=True) as dst:
                            while chunk := src.read(1024 * 1024):
                                dst.write(chunk)
            base = posixpath.basename(self.normalize(path).rstrip("/")) or "root"
            return out, f"{base}.zip"
        finally:
            tar.unlink(missing_ok=True)
