"""Quy tắc bù cho những chỗ Nixpacks đoán sai, áp dụng trước khi build (không dùng AI).

- Python: Nixpacks chỉ nhận `main.py`, Django hoặc Procfile; repo Flask/FastAPI
  đặt app trong `app.py`, `server.py`... thì lỗi "No start command could be found".
- Node: Nixpacks 1.41 mặc định Node 18 (đã hết hạn, bị gỡ khỏi nixpkgs); coi mọi
  repo Vite là trang tĩnh (Caddy) dù repo có server riêng; và dùng Bun khi có
  bun.lock, mà Bun cần CPU có AVX2.
Lệnh đoán ra luôn lắng nghe 0.0.0.0:$PORT (cổng panel cấp cho container).
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import t

from .i18n import t

from .i18n import t

from .i18n import t

# Thứ tự ưu tiên khi repo có nhiều file
ENTRY_FILES = ("app.py", "main.py", "server.py", "wsgi.py", "asgi.py", "application.py", "run.py")
PORT = "${PORT:-3000}"
MAX_SOURCE = 512 * 1024


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")[:MAX_SOURCE] if path.is_file() else ""
    except OSError:
        return ""


def guess_start_command(workspace: Path) -> tuple[str, str] | None:
    """Trả (lệnh, lý do) hoặc None nếu không đoán được."""
    deps = (_read(workspace / "requirements.txt") + _read(workspace / "pyproject.toml")).lower()
    for name in ENTRY_FILES:
        source = _read(workspace / name)
        if not source:
            continue
        module = name.removesuffix(".py")

        fastapi = re.search(r"^(\w+)\s*=\s*FastAPI\(", source, re.M)
        if fastapi and "uvicorn" in deps:
            return f"uvicorn {module}:{fastapi[1]} --host 0.0.0.0 --port {PORT}", t("detect.fastapi", file=name)

        flask = re.search(r"^(\w+)\s*=\s*Flask\(", source, re.M)
        if flask:
            target = f"{module}:{flask[1]}"
            if "gunicorn" in deps:
                return f"gunicorn --bind 0.0.0.0:{PORT} {target}", t("detect.flask_gunicorn", file=name)
            return (
                f"flask --app {target} run --host 0.0.0.0 --port {PORT}",
                t("detect.flask_dev", file=name),
            )

        if re.search(r"""^if\s+__name__\s*==\s*["']__main__["']""", source, re.M):
            return f"python {name}", t("detect.python_main", file=name)
    return None


# ---------- Node ----------

NODE_LTS = "22"
NPM_INSTALL = "npm install --no-audit --no-fund --legacy-peer-deps"


def cpu_flags(host_proc: Path) -> set[str] | None:
    """Cờ CPU của máy chủ; None nếu không đọc được hoặc không phải x86."""
    try:
        info = (host_proc / "cpuinfo").read_text()
    except OSError:
        return None
    for line in info.splitlines():
        if line.startswith("flags"):
            return set(line.split(":", 1)[-1].split())
    return None


def _package_json(workspace: Path) -> dict | None:
    try:
        data = json.loads(_read(workspace / "package.json") or "null")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


@dataclass
class Tuning:
    """Biến NIXPACKS_* và lệnh ghi đè cho `nixpacks build`, kèm lý do để ghi log."""

    env: dict[str, str] = field(default_factory=dict)
    install: str | None = None
    build: str | None = None
    start: str | None = None
    notes: list[str] = field(default_factory=list)


def node_version_env(workspace: Path, user_env: dict[str, str]) -> Tuning:
    """Áp dụng trước khi lập kế hoạch: chọn bản Node còn được hỗ trợ."""
    tuning = Tuning()
    pkg = _package_json(workspace)
    if pkg is None or "NIXPACKS_NODE_VERSION" in user_env:
        return tuning
    declared = (pkg.get("engines") or {}).get("node") if isinstance(pkg.get("engines"), dict) else None
    if not declared and not (workspace / ".nvmrc").is_file() and not (workspace / ".node-version").is_file():
        tuning.env["NIXPACKS_NODE_VERSION"] = NODE_LTS
        tuning.notes.append(t("detect.node_version", version=NODE_LTS))
    return tuning


def spa_env(workspace: Path, plan: dict, user_env: dict[str, str]) -> Tuning:
    """Repo có script start (server riêng) thì không phục vụ như trang tĩnh bằng Caddy."""
    tuning = Tuning()
    pkg = _package_json(workspace) or {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    start = (plan.get("start") or {}).get("cmd") or ""
    if "caddy" in start and scripts.get("start") and "NIXPACKS_SPA_CADDY" not in user_env:
        tuning.env["NIXPACKS_SPA_CADDY"] = "false"
        tuning.notes.append(t("detect.spa_start", script=scripts["start"]))
    return tuning


def bun_fallback(workspace: Path, plan: dict, flags: set[str] | None) -> Tuning:
    """Bun (bản thường) cần AVX2; CPU không có thì cài và chạy bằng npm."""
    tuning = Tuning()
    phases = plan.get("phases") or {}
    pkgs = (phases.get("setup") or {}).get("nixPkgs") or []
    install = (phases.get("install") or {}).get("cmds") or []
    uses_bun = "bun" in pkgs or any(str(c).startswith("bun ") for c in install)
    if not uses_bun or flags is None or "avx2" in flags:
        return tuning
    pkg = _package_json(workspace) or {}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    tuning.install = NPM_INSTALL
    tuning.build = "npm run build" if scripts.get("build") else None
    tuning.start = "npm start" if scripts.get("start") else None
    tuning.notes.append(t("detect.bun_no_avx2"))
    return tuning


# ---------- Gợi ý database ----------

# File đọc từ repo để đoán database; thứ tự = độ tin cậy giảm dần
SUGGEST_FILES = (
    "prisma/schema.prisma", ".env.example", "docker-compose.yml", "docker-compose.yaml", "compose.yaml",
    "package.json", "requirements.txt", "pyproject.toml", "Pipfile", "composer.json", "Gemfile", "go.mod",
)
_DRIVERS: dict[str, list[tuple[str, str]]] = {
    # file -> [(tên thư viện, engine)]
    "package.json": [("pg", "postgres"), ("postgres", "postgres"), ("pg-promise", "postgres"), ("mysql2", "mysql"),
                     ("mysql", "mysql"), ("mariadb", "mariadb"), ("mongoose", "mongo"), ("mongodb", "mongo"),
                     ("ioredis", "redis"), ("redis", "redis"), ("@redis/client", "redis")],
    "python": [("psycopg", "postgres"), ("psycopg2", "postgres"), ("psycopg2-binary", "postgres"),
               ("asyncpg", "postgres"), ("pymysql", "mysql"), ("mysqlclient", "mysql"),
               ("mysql-connector-python", "mysql"), ("aiomysql", "mysql"), ("mariadb", "mariadb"),
               ("pymongo", "mongo"), ("motor", "mongo"), ("mongoengine", "mongo"), ("redis", "redis"),
               ("django-redis", "redis")],
    "Gemfile": [("pg", "postgres"), ("mysql2", "mysql"), ("mongoid", "mongo"), ("redis", "redis")],
    "go.mod": [("github.com/lib/pq", "postgres"), ("github.com/jackc/pgx", "postgres"),
               ("github.com/go-sql-driver/mysql", "mysql"), ("go.mongodb.org/mongo-driver", "mongo"),
               ("github.com/redis/go-redis", "redis"), ("github.com/go-redis/redis", "redis")],
}
_URL_SCHEMES = (("postgres", "postgres"), ("mysql", "mysql"), ("mariadb", "mariadb"), ("mongodb", "mongo"))
_PRISMA = {"postgresql": "postgres", "cockroachdb": "postgres", "mysql": "mysql", "mongodb": "mongo"}


def _has_package(text: str, name: str, kind: str) -> bool:
    if kind == "package.json":
        return f'"{name}"' in text
    if kind == "go.mod":
        # Module Go có hậu tố phiên bản: github.com/jackc/pgx/v5
        return re.search(rf"{re.escape(name)}(?:/v\d+)?\s", text) is not None
    return re.search(rf"(?<![\w./-]){re.escape(name)}(?![\w./-])", text, re.I) is not None


def suggest_database(files: dict[str, str]) -> dict:
    """Đoán database repo cần từ file cấu hình/dependency (không dùng AI).

    Trả {"engine": key | None, "reason": str | None, "redis": bool}.
    """
    found: list[tuple[str, str]] = []  # (engine, lý do), theo độ tin cậy

    schema = files.get("prisma/schema.prisma", "")
    if m := re.search(r"datasource\s+\w+\s*{[^}]*provider\s*=\s*\"(\w+)\"", schema, re.S):
        if m[1] in _PRISMA:
            found.append((_PRISMA[m[1]], t("detect.db_prisma", provider=m[1])))

    env = files.get(".env.example", "")
    if m := re.search(r"^\s*(?:DATABASE_URL|DB_URL|MONGO(?:DB)?_URI)\s*=\s*[\"']?(\w+)(?:\+\w+)?://", env, re.M | re.I):
        for scheme, engine in _URL_SCHEMES:
            if m[1].lower().startswith(scheme):
                found.append((engine, t("detect.db_env_url", scheme=m[1])))
                break

    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yaml"):
        for m in re.finditer(r"image:\s*[\"']?(?:[\w.-]+/)?(postgres|mysql|mariadb|mongo|redis)\b", files.get(name, "")):
            found.append(({"mongo": "mongo"}.get(m[1], m[1]), t("detect.db_compose_image", file=name, image=m[1])))

    sources = [("package.json", "package.json")]
    sources += [(f, "python") for f in ("requirements.txt", "pyproject.toml", "Pipfile")]
    sources += [("Gemfile", "Gemfile"), ("go.mod", "go.mod")]
    for filename, kind in sources:
        text = files.get(filename, "")
        if not text:
            continue
        for package, engine in _DRIVERS[kind]:
            if _has_package(text, package, kind):
                found.append((engine, t("detect.db_package", file=filename, package=package)))

    composer = files.get("composer.json", "")
    if '"laravel/framework"' in composer:
        found.append(("mysql", t("detect.db_laravel")))
    elif '"wordpress' in composer.lower():
        found.append(("mariadb", t("detect.db_wordpress")))

    primary = next(((e, r) for e, r in found if e != "redis"), None)
    return {
        "engine": primary[0] if primary else None,
        "reason": primary[1] if primary else None,
        "redis": any(e == "redis" for e, _ in found),
    }
