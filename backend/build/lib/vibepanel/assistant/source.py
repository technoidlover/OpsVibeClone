"""Mã nguồn của service mà AI Agent sửa: file nào là cấu hình, lấy diff thành patch."""

import fnmatch
import subprocess
from pathlib import Path

from ..i18n import t

MAX_DIFF = 200_000

# Tên file (không tính thư mục) được coi là cấu hình; AI Agent tự sửa được các file này, file khác cần người dùng duyệt
CONFIG_FILES = {
    "Dockerfile", ".dockerignore", "compose.yml", "compose.yaml", "docker-compose.yml", "docker-compose.yaml",
    "nixpacks.toml", "nixpacks.json", "Procfile", "Caddyfile",
    ".nvmrc", ".node-version", ".python-version", "runtime.txt", ".tool-versions",
    "package.json", "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "pnpm-workspace.yaml", "bun.lock", "bun.lockb", ".npmrc", ".yarnrc.yml",
    "requirements.txt", "pyproject.toml", "poetry.lock", "uv.lock", "Pipfile", "Pipfile.lock", "setup.cfg",
    "composer.json", "composer.lock", "Gemfile", "Gemfile.lock", "go.mod", "go.sum", "Cargo.toml", "Cargo.lock",
    "tsconfig.json", "jsconfig.json", "angular.json", "vercel.json", "netlify.toml", ".babelrc",
    "gunicorn.conf.py", "uwsgi.ini", ".env.example",
    # PHP / WordPress
    "wp-config.php", ".htaccess", "nginx.template.conf", "php.ini", ".user.ini",
}
CONFIG_GLOBS = (
    "Dockerfile.*", "*.Dockerfile", "requirements*.txt", "tsconfig.*.json",
    "vite.config.*", "next.config.*", "nuxt.config.*", "astro.config.*", "svelte.config.*",
    "remix.config.*", "webpack.config.*", "babel.config.*", "postcss.config.*", "tailwind.config.*",
)


def is_config_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in CONFIG_FILES or any(fnmatch.fnmatchcase(name, pattern) for pattern in CONFIG_GLOBS)


def fingerprint(workspace: Path) -> bytes:
    """Toàn bộ thay đổi so với commit (kể cả file mới): so trước/sau để biết agent có thật sự đổi gì."""
    git = ["git", "-c", "safe.directory=*", "-C", str(workspace)]
    subprocess.run([*git, "add", "--intent-to-add", "--all"], capture_output=True, timeout=60)
    return subprocess.run([*git, "diff", "--binary"], capture_output=True, timeout=120).stdout


def collect_diff(workspace: Path, patch_path: Path) -> str:
    """Ghi patch đầy đủ ra file (để áp dụng khi deploy) và trả bản rút gọn để hiển thị."""
    git = ["git", "-c", "safe.directory=*", "-C", str(workspace)]
    # Cho file mới tạo xuất hiện trong diff
    subprocess.run([*git, "add", "--intent-to-add", "--all"], capture_output=True, timeout=60)
    result = subprocess.run([*git, "diff", "--binary"], capture_output=True, timeout=120)
    patch = result.stdout
    if patch:
        patch_path.write_bytes(patch)
    else:
        patch_path.unlink(missing_ok=True)
    diff = patch.decode(errors="replace")
    if len(diff) > MAX_DIFF:
        diff = diff[:MAX_DIFF] + "\n" + t("assistant.diff_truncated") + "\n"
    return diff
