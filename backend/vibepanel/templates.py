"""Ứng dụng 1-click (WordPress, Ghost, n8n...) chạy bằng image Docker chính thức.

Bản legacy clone mã nguồn rồi build bằng Nixpacks (WordPress chạy `php -S`, chỉ
là server dev). Ở đây mỗi ứng dụng là một service dạng image: panel kéo image,
tạo database trắng nếu ứng dụng cần, gắn volume giữ dữ liệu và điền sẵn biến
môi trường. Người dùng sửa biến được như service thường.

Biến có `${VP_URL}` / `${VP_DOMAIN}` được thay lúc khởi động container bằng
địa chỉ hiện tại của service, nên đổi tên miền chỉ cần deploy lại.
"""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field

from .databases import ENGINES
from .models import Database

URL_VAR = "${VP_URL}"
DOMAIN_VAR = "${VP_DOMAIN}"


@dataclass(frozen=True)
class Template:
    key: str
    label: str
    # category, description, next_step là khóa i18n (vibepanel/i18n/core.py), dịch lúc trả về API
    category: str
    description: str
    image: str
    port: int
    # Thư mục giữ dữ liệu (mỗi thư mục một Docker volume)
    volumes: tuple[str, ...]
    env: Callable[[Database | None, str], dict[str, str]]
    # Database tạo kèm: (engine, phiên bản); None = không cần
    database: tuple[str, str] | None = None
    # Ứng dụng cần biết địa chỉ công khai của mình (link trong email, webhook...)
    needs_url: bool = False
    website: str = ""
    # Ghi chú hiện sau khi tạo (bước tiếp theo người dùng cần làm), khóa i18n
    next_step: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def db_label(self) -> str | None:
        return f"{ENGINES[self.database[0]].label} {self.database[1]}" if self.database else None


def _secret(n: int = 32) -> str:
    return secrets.token_hex(n)


def _wordpress(db: Database | None, password: str) -> dict[str, str]:
    assert db is not None
    return {
        "WORDPRESS_DB_HOST": f"{db.host}:{ENGINES[db.engine].port}",
        "WORDPRESS_DB_NAME": db.db_name,
        "WORDPRESS_DB_USER": db.username,
        "WORDPRESS_DB_PASSWORD": password,
        # Sau Traefik: nhận HTTPS từ header để không lặp chuyển hướng; cài plugin không cần FTP
        "WORDPRESS_CONFIG_EXTRA": (
            "if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') "
            "{ $_SERVER['HTTPS'] = 'on'; } define('FS_METHOD', 'direct');"
        ),
    }


def _ghost(db: Database | None, password: str) -> dict[str, str]:
    assert db is not None
    return {
        "url": URL_VAR,
        "database__client": "mysql",
        "database__connection__host": db.host,
        "database__connection__port": str(ENGINES[db.engine].port),
        "database__connection__user": db.username,
        "database__connection__password": password,
        "database__connection__database": db.db_name,
    }


def _n8n(db: Database | None, password: str) -> dict[str, str]:
    assert db is not None
    return {
        "DB_TYPE": "postgresdb",
        "DB_POSTGRESDB_HOST": db.host,
        "DB_POSTGRESDB_PORT": str(ENGINES[db.engine].port),
        "DB_POSTGRESDB_DATABASE": db.db_name,
        "DB_POSTGRESDB_USER": db.username,
        "DB_POSTGRESDB_PASSWORD": password,
        "N8N_HOST": DOMAIN_VAR,
        "N8N_PROTOCOL": "https",
        "WEBHOOK_URL": URL_VAR + "/",
        "N8N_PROXY_HOPS": "1",
        "N8N_ENCRYPTION_KEY": _secret(),
        "GENERIC_TIMEZONE": "Asia/Ho_Chi_Minh",
        "TZ": "Asia/Ho_Chi_Minh",
    }


def _gitea(db: Database | None, password: str) -> dict[str, str]:
    assert db is not None
    return {
        "GITEA__database__DB_TYPE": "postgres",
        "GITEA__database__HOST": f"{db.host}:{ENGINES[db.engine].port}",
        "GITEA__database__NAME": db.db_name,
        "GITEA__database__USER": db.username,
        "GITEA__database__PASSWD": password,
        "GITEA__server__ROOT_URL": URL_VAR + "/",
        "GITEA__server__DOMAIN": DOMAIN_VAR,
        "GITEA__server__HTTP_PORT": "3000",
        # Clone qua HTTPS; SSH cần mở thêm cổng nên tắt mặc định
        "GITEA__server__DISABLE_SSH": "true",
        "GITEA__security__INSTALL_LOCK": "false",
    }


def _none(_db: Database | None, _password: str) -> dict[str, str]:
    return {}


TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (
        Template(
            "wordpress", "WordPress", "tpl.category.cms", "tpl.wordpress.description",
            "wordpress:php8.3-apache", 80, ("/var/www/html",), _wordpress,
            database=("mariadb", "11.4"), website="https://wordpress.org",
            next_step="tpl.wordpress.next_step",
            tags=("php",),
        ),
        Template(
            "ghost", "Ghost", "tpl.category.cms", "tpl.ghost.description",
            "ghost:6-alpine", 2368, ("/var/lib/ghost/content",), _ghost,
            database=("mysql", "8.0"), needs_url=True, website="https://ghost.org",
            next_step="tpl.ghost.next_step",
            tags=("node",),
        ),
        Template(
            "n8n", "n8n", "tpl.category.automation", "tpl.n8n.description",
            "n8nio/n8n:stable", 5678, ("/home/node/.n8n",), _n8n,
            database=("postgres", "17"), needs_url=True, website="https://n8n.io",
            next_step="tpl.n8n.next_step",
            tags=("node",),
        ),
        Template(
            "uptime-kuma", "Uptime Kuma", "tpl.category.monitoring", "tpl.uptime-kuma.description",
            "louislam/uptime-kuma:1", 3001, ("/app/data",), _none,
            website="https://uptime.kuma.pet",
            next_step="tpl.uptime-kuma.next_step",
        ),
        Template(
            "gitea", "Gitea", "tpl.category.devtools", "tpl.gitea.description",
            "gitea/gitea:1", 3000, ("/data",), _gitea,
            database=("postgres", "17"), needs_url=True, website="https://about.gitea.com",
            next_step="tpl.gitea.next_step",
        ),
        Template(
            "adminer", "Adminer", "tpl.category.devtools", "tpl.adminer.description",
            "adminer:latest", 8080, (), _none,
            website="https://www.adminer.org",
            next_step="tpl.adminer.next_step",
        ),
    )
}


def volume_name(slug: str, mount: str) -> str:
    """Tên Docker volume ổn định theo service và thư mục (không đổi khi thêm/bớt thư mục khác)."""
    return f"vp_{slug}_" + (mount.strip("/").replace("/", "-") or "root")


def render_env(env: dict[str, str], url: str | None) -> dict[str, str]:
    """Thay ${VP_URL} / ${VP_DOMAIN} bằng địa chỉ hiện tại của service."""
    domain = url.removeprefix("https://") if url else ""
    return {k: v.replace(URL_VAR, url or "").replace(DOMAIN_VAR, domain) for k, v in env.items()}
