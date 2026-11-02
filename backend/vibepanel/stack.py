"""Nhận diện cấu trúc repo cho fullstack / monorepo (không dùng AI).

Từ danh sách file của repo (Git Trees API) và nội dung vài file nhỏ (package.json,
.env.example...), đoán thư mục nào là frontend, thư mục nào là backend, framework
gì, và tên biến môi trường nên dùng để nối hai bên:
- frontend: biến chứa URL của API (VITE_API_URL, NEXT_PUBLIC_API_URL...), ưu tiên tên
  đã có trong .env.example của repo;
- backend: biến chứa địa chỉ frontend để mở CORS (CORS_ORIGIN, FRONTEND_URL...).
Panel không sửa mã nguồn: chỉ đặt các biến này với giá trị ${tên-service.URL}.
"""

import json
import re

from .detect import suggest_database

# File đánh dấu một thư mục là app riêng
MARKERS = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "composer.json", "Gemfile",
           "pom.xml", "build.gradle", "build.gradle.kts", "Cargo.toml", "Dockerfile", "manage.py")
# File đọc nội dung để phân loại (nhỏ)
READ_FILES = ("package.json", ".env.example", "requirements.txt", "pyproject.toml", "composer.json", "go.mod")
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "vendor", ".next", "venv", ".venv", "docs", "examples",
             "test", "tests", "e2e", "scripts", ".github", "public", "static", "assets"}
MAX_DEPTH = 2

FE_NAMES = ("frontend", "client", "web", "ui", "fe", "app", "www", "site", "dashboard", "admin")
BE_NAMES = ("backend", "server", "api", "be", "service", "core", "worker")

# (gói npm, framework, biến URL API mặc định); thứ tự: gói cụ thể trước
FE_FRAMEWORKS = (
    ("next", "Next.js", "NEXT_PUBLIC_API_URL"),
    ("nuxt", "Nuxt", "NUXT_PUBLIC_API_BASE"),
    ("@sveltejs/kit", "SvelteKit", "PUBLIC_API_URL"),
    ("astro", "Astro", "PUBLIC_API_URL"),
    ("@angular/core", "Angular", None),
    ("react-scripts", "Create React App", "REACT_APP_API_URL"),
    ("@vue/cli-service", "Vue CLI", "VUE_APP_API_URL"),
    ("vite", "Vite", "VITE_API_URL"),
)
BE_PACKAGES = (
    ("@nestjs/core", "NestJS"), ("express", "Express"), ("fastify", "Fastify"), ("koa", "Koa"),
    ("@hapi/hapi", "hapi"), ("hono", "Hono"), ("@adonisjs/core", "AdonisJS"), ("socket.io", "Socket.IO"),
)
# Tiền tố biến mà framework frontend nhúng vào mã lúc build
FE_PREFIXES = ("VITE_", "NEXT_PUBLIC_", "REACT_APP_", "VUE_APP_", "NUXT_PUBLIC_", "PUBLIC_", "NG_APP_")

_ENV_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", re.MULTILINE)
_API_KEY = re.compile(r"(API|BACKEND|SERVER)[A-Z_]*(URL|URI|HOST|BASE|ENDPOINT|ORIGIN)", re.IGNORECASE)
_CORS_KEY = re.compile(r"CORS|FRONTEND|CLIENT_(URL|ORIGIN)|ALLOWED_ORIGIN|WEB_URL|APP_URL|ORIGIN", re.IGNORECASE)


def candidate_dirs(paths: list[str]) -> list[str]:
    """Các thư mục (độ sâu <= 2, "" = gốc) có file đánh dấu app."""
    dirs: set[str] = set()
    for path in paths:
        parts = path.split("/")
        if parts[-1] not in MARKERS or len(parts) - 1 > MAX_DEPTH:
            continue
        if any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
            continue
        dirs.add("/".join(parts[:-1]))
    return sorted(dirs, key=lambda d: (d.count("/"), d))


def _deps(package_json: str | None) -> dict:
    try:
        data = json.loads(package_json or "")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}


def _env_keys(text: str | None) -> list[str]:
    return list(dict.fromkeys(_ENV_KEY.findall(text or "")))


def classify(path: str, files: dict[str, str]) -> dict:
    """Phân loại một thư mục từ nội dung các file trong READ_FILES (đường dẫn tương đối trong thư mục)."""
    name = path.rsplit("/", 1)[-1].lower()
    deps = _deps(files.get("package.json"))
    env_keys = _env_keys(files.get(".env.example"))
    kind = framework = None
    api_vars: list[str] = []
    cors_vars: list[str] = []

    for package, label, var in FE_FRAMEWORKS:
        if package in deps:
            kind, framework = "frontend", label
            # Biến URL API đã có sẵn trong .env.example (đúng tiền tố framework) được ưu tiên
            api_vars = [k for k in env_keys if _API_KEY.search(k) and k.startswith(FE_PREFIXES)]
            if not api_vars and var:
                api_vars = [var]
            break
    backend_lib = next((label for package, label in BE_PACKAGES if package in deps), None)
    if backend_lib and (kind is None or framework in ("Vite",) and name in BE_NAMES):
        kind, framework = "backend", backend_lib
    if kind is None:
        if "requirements.txt" in files or "pyproject.toml" in files:
            kind, framework = "backend", _python_framework(files)
        elif "go.mod" in files:
            kind, framework = "backend", "Go"
        elif "composer.json" in files:
            kind, framework = "backend", "Laravel" if "laravel/framework" in files["composer.json"] else "PHP"
        elif name in BE_NAMES:
            kind = "backend"
        elif name in FE_NAMES:
            kind = "frontend"
    if kind == "backend":
        cors_vars = [k for k in env_keys if _CORS_KEY.search(k) and not _API_KEY.search(k)]
        if not cors_vars:
            cors_vars = ["CORS_ORIGIN", "FRONTEND_URL"]
    database = suggest_database(files) if kind == "backend" else {"engine": None, "reason": None, "redis": False}
    return {
        "path": path, "kind": kind, "framework": framework, "api_vars": api_vars, "cors_vars": cors_vars,
        "database": database["engine"], "database_reason": database["reason"], "redis": database["redis"],
    }


def _python_framework(files: dict[str, str]) -> str:
    text = (files.get("requirements.txt", "") + files.get("pyproject.toml", "")).lower()
    for package, label in (("django", "Django"), ("fastapi", "FastAPI"), ("flask", "Flask")):
        if package in text:
            return label
    return "Python"
