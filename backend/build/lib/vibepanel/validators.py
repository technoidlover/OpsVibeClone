"""Kiểm tra đầu vào trước khi chúng chạm tới git, docker hay hệ thống file.

Tên project/service đi thẳng vào tên container, tên thư mục và label Traefik,
nên chỉ cho phép ký tự an toàn tuyệt đối.
"""

import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator

from .i18n import t

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RESERVED_NAMES = {"opsvibes", "vibepanel", "traefik"}


def validate_name(value: str) -> str:
    if not _NAME_RE.fullmatch(value):
        raise ValueError(t("validators.name"))
    if value in RESERVED_NAMES:
        raise ValueError(t("validators.name_reserved", name=value))
    return value


def validate_domain(value: str) -> str:
    value = value.strip().lower().rstrip(".")
    if not _DOMAIN_RE.fullmatch(value):
        raise ValueError(t("validators.domain"))
    return value


def validate_repo_url(value: str) -> str:
    value = value.strip()
    parts = urlsplit(value)
    if parts.scheme != "https":
        raise ValueError(t("validators.repo_https"))
    if not parts.hostname or parts.username or parts.password:
        raise ValueError(t("validators.repo_credentials"))
    if parts.query or parts.fragment:
        raise ValueError(t("validators.repo_query"))
    if not parts.path.strip("/"):
        raise ValueError(t("validators.repo_path"))
    return value


def validate_branch(value: str) -> str:
    if (
        not _BRANCH_RE.fullmatch(value)
        or value.startswith(("-", "/"))
        or value.endswith(("/", ".lock", "."))
        or ".." in value
        or "//" in value
    ):
        raise ValueError(t("validators.branch"))
    return value


def validate_email(value: str) -> str:
    value = value.strip().lower()
    if not _EMAIL_RE.fullmatch(value) or len(value) > 255:
        raise ValueError(t("validators.email"))
    return value


def validate_env(value: dict[str, str]) -> dict[str, str]:
    for key, val in value.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(t("validators.env_key", var=repr(key)))
        if "\x00" in val:
            raise ValueError(t("validators.env_nul", var=key))
    return value


Name = Annotated[str, AfterValidator(validate_name)]
Domain = Annotated[str, AfterValidator(validate_domain)]
RepoUrl = Annotated[str, AfterValidator(validate_repo_url)]
Branch = Annotated[str, AfterValidator(validate_branch)]
Email = Annotated[str, AfterValidator(validate_email)]
EnvVars = Annotated[dict[str, str], AfterValidator(validate_env)]


def validate_start_command(value: str) -> str:
    value = value.strip()
    if len(value) > 500:
        raise ValueError(t("validators.start_command_length"))
    if any(ch in value for ch in "\r\n\x00"):
        raise ValueError(t("validators.start_command_line"))
    return value


StartCommand = Annotated[str, AfterValidator(validate_start_command)]


_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]{0,254}$", re.IGNORECASE)
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9._/-]{1,200}$")


def validate_image(value: str) -> str:
    value = value.strip()
    if not _IMAGE_RE.fullmatch(value) or "//" in value:
        raise ValueError(t("validators.image"))
    return value


def validate_mounts(value: list[str]) -> list[str]:
    out: list[str] = []
    for mount in value:
        mount = mount.strip().rstrip("/")
        if not _MOUNT_RE.fullmatch(mount) or ".." in mount.split("/"):
            raise ValueError(t("validators.mount", mount=repr(mount)))
        if mount not in out:
            out.append(mount)
    if len(out) > 5:
        raise ValueError(t("validators.mounts_max"))
    return out


ImageRef = Annotated[str, AfterValidator(validate_image)]
Mounts = Annotated[list[str], AfterValidator(validate_mounts)]


_ROOT_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")


def validate_root_dir(value: str) -> str:
    """Thư mục con tương đối trong repo; "" = thư mục gốc."""
    value = value.strip().strip("/")
    if value in ("", "."):
        return ""
    if len(value) > 255 or not _ROOT_DIR_RE.fullmatch(value) or any(p in (".", "..") for p in value.split("/")):
        raise ValueError(t("validators.root_dir"))
    return value


RootDir = Annotated[str, AfterValidator(validate_root_dir)]
