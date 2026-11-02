"""Biến tham chiếu giữa các service trong cùng project (fullstack, monorepo).

Trong biến môi trường, `${api.URL}` được thay bằng địa chỉ của service `api` lúc
build và lúc khởi động, nên đổi tên miền của backend chỉ cần deploy lại frontend:

    ${api.URL}           https://api.example.com   (địa chỉ công khai)
    ${api.DOMAIN}        api.example.com
    ${api.HOST}          shop-api                  (host nội bộ, chỉ gọi được từ service khác)
    ${api.PORT}          8080
    ${api.INTERNAL_URL}  http://shop-api:8080      (gọi nội bộ, không qua Internet)

Database dùng chung cú pháp: ${main-db.HOST}, ${main-db.PORT}.
"""

import re
from collections.abc import Callable

from . import panel_settings
from .models import Database, Service

REF_RE = re.compile(r"\$\{([a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?)\.(URL|DOMAIN|HOST|PORT|INTERNAL_URL)\}")
DEFAULT_PORT = 3000


def public_host(service: Service, base_domain: str | None) -> str | None:
    return service.domain or panel_settings.default_service_domain(base_domain, service.project.name, service.name)


def resolve(
    env: dict[str, str], service: Service, base_domain: str | None, port_of: Callable[[Service], int | None],
) -> tuple[dict[str, str], list[str]]:
    """Thay các tham chiếu; trả (env mới, các tham chiếu không giải được để cảnh báo)."""
    project = service.project
    services = {s.name: s for s in project.services}
    databases = {d.name: d for d in project.databases}
    missing: list[str] = []

    def value(match: re.Match) -> str:
        name, field = match.group(1), match.group(2)
        target: Service | Database | None = services.get(name) or databases.get(name)
        if target is None:
            return _miss(match)
        if isinstance(target, Database):
            from .databases import ENGINES

            port = str(ENGINES[target.engine].port)
            return {"HOST": target.host, "PORT": port, "INTERNAL_URL": f"{target.host}:{port}"}.get(field) or _miss(match)
        host = public_host(target, base_domain)
        port = str(port_of(target) or DEFAULT_PORT)
        if field in ("URL", "DOMAIN") and not host:
            return _miss(match)
        return {
            "URL": f"https://{host}",
            "DOMAIN": host or "",
            "HOST": target.internal_host,
            "PORT": port,
            "INTERNAL_URL": f"http://{target.internal_host}:{port}",
        }[field]

    def _miss(match: re.Match) -> str:
        missing.append(match.group(0))
        return ""

    out = {k: REF_RE.sub(value, v) if "${" in v else v for k, v in env.items()}
    return out, missing
