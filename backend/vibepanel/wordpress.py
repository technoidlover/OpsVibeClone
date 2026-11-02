"""Tự cấu hình WordPress deploy từ repo / file nén (không cần AI).

Mã nguồn WordPress chuyển từ hosting khác thường có wp-config.php ghi cứng thông
tin database cũ (DB_HOST = localhost), kèm file dump .sql ngay trong thư mục web.
Trước khi build, panel:
- sửa wp-config.php để đọc DB_NAME / DB_USER / DB_PASSWORD / DB_HOST từ biến môi
  trường (giá trị cũ giữ làm dự phòng), hoặc tạo mới từ wp-config-sample.php;
  thêm khối nhận HTTPS sau Traefik và địa chỉ site theo tên miền đang truy cập
  (database chuyển sang vẫn trỏ siteurl về tên miền cũ);
- chuyển file dump .sql và error_log ra khỏi thư mục web (không thì ai cũng tải
  được), rồi import dump vào database liên kết nếu database đó chưa từng import;
- (chỉ khi build bằng Nixpacks) thêm nginx.template.conf cho permalink và chặn
  file nhạy cảm, cho PHP (user nobody) ghi được wp-content, giữ wp-content/uploads
  bằng volume để ảnh tải lên không mất khi deploy lại.
"""

import re
import secrets
import shutil
import string
from pathlib import Path

from .i18n import t

MARKER = "OpsVibes"
# Khối do bản trước (tên cũ VibePanel) chèn vào: đã có thì không chèn thêm
LEGACY_MARKER = "VibePanel"
UPLOADS_MOUNT = "/app/wp-content/uploads"
# Nixpacks chạy php-fpm bằng user nobody; mã nguồn được COPY vào image với chủ là root
CHOWN_PREFIX = "chown -R nobody:nobody /app/wp-content 2>/dev/null; "
DUMP_SUFFIXES = (".sql", ".sql.gz")

_DB_KEYS = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST")
_SALT_KEYS = ("AUTH_KEY", "SECURE_AUTH_KEY", "LOGGED_IN_KEY", "NONCE_KEY",
              "AUTH_SALT", "SECURE_AUTH_SALT", "LOGGED_IN_SALT", "NONCE_SALT")

_ENV_VALUE = {
    "DB_NAME": "getenv('DB_NAME')",
    "DB_USER": "getenv('DB_USER')",
    "DB_PASSWORD": "getenv('DB_PASSWORD')",
    # Biến DB_PORT do panel thêm khi liên kết database
    "DB_HOST": "(getenv('DB_HOST') ? getenv('DB_HOST') . (getenv('DB_PORT') ? ':' . getenv('DB_PORT') : '') : false)",
}

PROXY_BLOCK = f"""
/* --- {MARKER}: chạy sau reverse proxy (Traefik) --- */
if (isset($_SERVER['HTTP_X_FORWARDED_PROTO']) && $_SERVER['HTTP_X_FORWARDED_PROTO'] === 'https') {{
    $_SERVER['HTTPS'] = 'on';
}}
/* Địa chỉ site theo tên miền đang truy cập: database chuyển từ hosting khác vẫn lưu tên miền cũ */
if (!empty($_SERVER['HTTP_HOST'])) {{
    $vp_url = (!empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off' ? 'https' : 'http') . '://' . $_SERVER['HTTP_HOST'];
    if (!defined('WP_HOME')) define('WP_HOME', $vp_url);
    if (!defined('WP_SITEURL')) define('WP_SITEURL', $vp_url);
}}
if (!defined('FS_METHOD')) define('FS_METHOD', 'direct');
/* --- /{MARKER} --- */
"""

NGINX_TEMPLATE = r"""worker_processes 5;
daemon off;
worker_rlimit_nofile 8192;

events {
  worker_connections 4096;
}

http {
    include    $!{nginx}/conf/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout;
    error_log /dev/stderr;
    sendfile on;
    tcp_nopush on;
    server_names_hash_bucket_size 128;
    client_max_body_size 256m;

    server {
        listen ${PORT};
        listen [::]:${PORT};
        server_name localhost;
        root /app;
        index index.php index.html;
        charset utf-8;

        add_header X-Frame-Options "SAMEORIGIN";
        add_header X-Content-Type-Options "nosniff";

        # Do OpsVibes tạo cho WordPress: permalink và chặn file nhạy cảm
        location ~* (^|/)(wp-config\.php|wp-config-sample\.php|error_log|readme\.html|license\.txt|nginx\.template\.conf)$ { deny all; }
        location ~* \.(sql|sql\.gz|log|bak|old|orig|swp|env)$ { deny all; }
        location ~ /\.(?!well-known) { deny all; }
        location ~* ^/wp-content/uploads/.*\.php$ { deny all; }

        location / {
            try_files $uri $uri/ /index.php?$args;
        }

        location ~ \.php$ {
            try_files $uri =404;
            fastcgi_pass 127.0.0.1:9000;
            fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
            include $!{nginx}/conf/fastcgi_params;
            include $!{nginx}/conf/fastcgi.conf;
            fastcgi_read_timeout 300;
        }

        location ~* \.(css|js|png|jpe?g|gif|webp|avif|svg|ico|woff2?)$ {
            expires 7d;
            access_log off;
        }
    }
}
"""


def is_wordpress(workspace: Path) -> bool:
    return (workspace / "wp-settings.php").is_file() and (workspace / "wp-includes").is_dir()


def _salt() -> str:
    alphabet = string.ascii_letters + string.digits + "!@#%^&*()-_[]{}<>~+=,.;:/?|"
    return "".join(secrets.choice(alphabet) for _ in range(64))


def _define_re(key: str) -> re.Pattern:
    # define( 'DB_NAME', 'giá trị' );  hoặc  define("DB_NAME", "giá trị");
    return re.compile(rf"""define\s*\(\s*(['"]){key}\1\s*,\s*(?P<value>.+?)\s*\)\s*;""")


def patch_config(text: str) -> tuple[str, list[str]]:
    """Cho các hằng DB_* đọc biến môi trường (giữ giá trị cũ làm dự phòng); thêm khối proxy. Trả (nội dung, khóa đã sửa)."""
    changed: list[str] = []
    for key in _DB_KEYS:
        pattern = _define_re(key)
        match = pattern.search(text)
        if match is None or "getenv(" in match.group("value"):
            continue
        replacement = f"define('{key}', {_ENV_VALUE[key]} ?: {match.group('value')});"
        text = text[: match.start()] + replacement + text[match.end():]
        changed.append(key)
    if MARKER not in text and LEGACY_MARKER not in text:
        text = _insert_block(text, PROXY_BLOCK)
    return text, changed


def _insert_block(text: str, block: str) -> str:
    for anchor in (r"/\*\s*That's all, stop editing", r"require_once\s*\(?\s*ABSPATH\s*\.\s*['\"]wp-settings\.php"):
        match = re.search(anchor, text)
        if match:
            return text[: match.start()] + block.lstrip("\n") + "\n" + text[match.start():]
    # Không tìm thấy chỗ quen thuộc: chèn ngay sau thẻ mở PHP
    return re.sub(r"^<\?php\s*", lambda m: m.group(0) + block + "\n", text, count=1)


def config_from_sample(sample: str) -> str:
    text, _ = patch_config(sample)
    for key in _SALT_KEYS:
        text = _define_re(key).sub(lambda _m, k=key: f"define('{k}', '{_salt()}');", text, count=1)
    return text


def prepare(workspace: Path, stash: Path, nixpacks: bool, log) -> list[Path]:
    """Sửa cấu hình trong workspace; trả danh sách file dump đã chuyển vào `stash`."""
    config, sample = workspace / "wp-config.php", workspace / "wp-config-sample.php"
    if config.is_file():
        text, changed = patch_config(config.read_text(errors="surrogateescape"))
        config.write_text(text, errors="surrogateescape")
        if changed:
            log.write(t("wp.config_patched", keys=", ".join(changed)))
    elif sample.is_file():
        config.write_text(config_from_sample(sample.read_text(errors="surrogateescape")), errors="surrogateescape")
        log.write(t("wp.config_created"))

    dumps: list[Path] = []
    stash.mkdir(parents=True, exist_ok=True)
    for path in sorted(workspace.iterdir()):
        name = path.name.lower()
        if path.is_file() and (name.endswith(DUMP_SUFFIXES) or name == "error_log"):
            target = stash / path.name
            shutil.move(str(path), target)
            if name != "error_log":
                dumps.append(target)
            log.write(t("wp.moved_out", name=path.name))

    if nixpacks and not (workspace / "nginx.template.conf").exists():
        (workspace / "nginx.template.conf").write_text(NGINX_TEMPLATE)
        log.write(t("wp.nginx"))
    return dumps
