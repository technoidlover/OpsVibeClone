"""Database 1-click: PostgreSQL, MySQL, MariaDB, MongoDB, Redis chạy bằng image chính thức.

Mỗi database là một container riêng trong network `opsvibes` (app gọi bằng tên
host nội bộ, không mở cổng ra Internet) và một Docker volume giữ dữ liệu. Mật khẩu
chỉ đi qua biến môi trường của container, không nằm trên dòng lệnh. Lệnh kiểm tra
và import chạy trong container bằng `sh -c` đọc lại các biến đó.
"""

import gzip
import re
import secrets
import string
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from .crypto import SecretBox
from .i18n import t
from .i18n import t
from .i18n import t
from .i18n import t
from .models import Database

READY_TIMEOUT = 180
IMPORT_TIMEOUT = 3600
CHUNK = 1024 * 1024
DB_LABEL = "opsvibes.database"


@dataclass(frozen=True)
class Engine:
    key: str
    label: str
    image: str
    versions: tuple[str, ...]
    port: int
    data_dir: str
    # Có định dạng backup để import không
    importable: bool = True

    @property
    def default_version(self) -> str:
        return self.versions[0]


ENGINES: dict[str, Engine] = {
    e.key: e
    for e in (
        Engine("postgres", "PostgreSQL", "postgres", ("17", "16", "15"), 5432, "/var/lib/postgresql/data"),
        Engine("mysql", "MySQL", "mysql", ("8.4", "8.0"), 3306, "/var/lib/mysql"),
        Engine("mariadb", "MariaDB", "mariadb", ("11.4", "10.11"), 3306, "/var/lib/mysql"),
        Engine("mongo", "MongoDB", "mongo", ("8.0", "7.0"), 27017, "/data/db"),
        Engine("redis", "Redis", "redis", ("7.4",), 6379, "/data", importable=False),
    )
}


def generate_password(length: int = 24) -> str:
    # Chỉ chữ và số: không cần escape trong URL, file .env hay lệnh shell
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def default_names(db: Database) -> tuple[str, str]:
    """(tên database, user) mặc định theo tên service database."""
    base = db.name.replace("-", "_")[:30]
    return base, "root" if db.engine == "mongo" else ("default" if db.engine == "redis" else base[:16])


# ---------- Container ----------

def image(db: Database) -> str:
    return f"{ENGINES[db.engine].image}:{db.version}"


def container_env(db: Database, box: SecretBox) -> dict[str, str]:
    password = box.decrypt(db.password_enc)
    root = box.decrypt_optional(db.root_password_enc) or password
    if db.engine == "postgres":
        return {"POSTGRES_DB": db.db_name, "POSTGRES_USER": db.username, "POSTGRES_PASSWORD": password}
    if db.engine in ("mysql", "mariadb"):
        p = db.engine.upper()
        return {f"{p}_DATABASE": db.db_name, f"{p}_USER": db.username, f"{p}_PASSWORD": password,
                f"{p}_ROOT_PASSWORD": root}
    if db.engine == "mongo":
        return {"MONGO_INITDB_ROOT_USERNAME": db.username, "MONGO_INITDB_ROOT_PASSWORD": password,
                "MONGO_INITDB_DATABASE": db.db_name}
    return {"REDIS_PASSWORD": password}


def container_cmd(db: Database) -> list[str] | None:
    if db.engine == "redis":
        return ["sh", "-c", 'exec redis-server --requirepass "$REDIS_PASSWORD" --appendonly yes']
    return None


def health_cmd(db: Database) -> list[str]:
    """Lệnh trả mã 0 khi database đã nhận kết nối qua TCP (không phải server tạm lúc khởi tạo)."""
    script = {
        "postgres": 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "select 1"',
        "mysql": 'MYSQL_PWD="$MYSQL_PASSWORD" mysql -h 127.0.0.1 -u "$MYSQL_USER" "$MYSQL_DATABASE" -e "select 1"',
        "mariadb": 'MYSQL_PWD="$MARIADB_PASSWORD" mariadb -h 127.0.0.1 -u "$MARIADB_USER" "$MARIADB_DATABASE" -e "select 1"',
        "mongo": 'mongosh --quiet --host 127.0.0.1 -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
                 '--authenticationDatabase admin --eval "db.adminCommand({ping: 1}).ok"',
        "redis": 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli -h 127.0.0.1 ping | grep -q PONG',
    }[db.engine]
    return ["sh", "-c", script]


# ---------- Kết nối ----------

def connection_url(db: Database, password: str) -> str:
    pw = quote(password, safe="")
    host, port = db.host, ENGINES[db.engine].port
    if db.engine == "postgres":
        return f"postgresql://{db.username}:{pw}@{host}:{port}/{db.db_name}"
    if db.engine in ("mysql", "mariadb"):
        return f"mysql://{db.username}:{pw}@{host}:{port}/{db.db_name}"
    if db.engine == "mongo":
        return f"mongodb://{db.username}:{pw}@{host}:{port}/{db.db_name}?authSource=admin"
    return f"redis://default:{pw}@{host}:{port}"


def link_env(db: Database, password: str) -> dict[str, str]:
    """Biến môi trường thêm vào service khi bấm "Kết nối với service"."""
    url = connection_url(db, password)
    port = str(ENGINES[db.engine].port)
    if db.engine == "redis":
        return {"REDIS_URL": url, "REDIS_HOST": db.host, "REDIS_PORT": port, "REDIS_PASSWORD": password}
    if db.engine == "mongo":
        return {"MONGODB_URI": url, "MONGO_URL": url, "DATABASE_URL": url}
    return {
        "DATABASE_URL": url, "DB_HOST": db.host, "DB_PORT": port,
        "DB_USER": db.username, "DB_PASSWORD": password, "DB_NAME": db.db_name,
    }


# ---------- Import backup ----------

def detect_format(path: Path, filename: str) -> tuple[bool, str]:
    """(file nén gzip?, định dạng: sql | pgdump | mongo-archive | mongo-json | mongo-json-array)."""
    with path.open("rb") as f:
        head = f.read(64)
    gz = head[:2] == b"\x1f\x8b"
    if gz:
        with gzip.open(path, "rb") as f:
            head = f.read(64)
    name = filename.lower().removesuffix(".gz")
    if head[:5] == b"PGDMP":
        return gz, "pgdump"
    if name.endswith((".archive", ".bson", ".mongo")) or head[:4] == b"\x6d\xe2\x99\x81":
        return gz, "mongo-archive"
    if name.endswith((".json", ".jsonl", ".ndjson")):
        # Mảng JSON (mongoexport --jsonArray, file xuất từ Compass...) hoặc mỗi dòng một document
        return gz, "mongo-json-array" if head.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"[") else "mongo-json"
    return gz, "sql"


def collection_name(filename: str) -> str:
    """Collection nhận dữ liệu JSON: theo tên file (users.json -> users)."""
    stem = Path(filename).name.lower().removesuffix(".gz")
    for ext in (".json", ".jsonl", ".ndjson"):
        stem = stem.removesuffix(ext)
    stem = re.sub(r"[^a-z0-9_.-]", "_", stem).strip("._-")
    return stem[:100] or "data"


def import_cmd(db: Database, fmt: str, filename: str = "", clean: bool = False) -> list[str]:
    """Lệnh đọc backup từ stdin và nạp vào database (chạy bằng `docker exec -i`).

    clean: khôi phục bản sao lưu, xóa bảng đang có trước khi tạo lại (dump MySQL/MariaDB đã có sẵn
    DROP TABLE, mongorestore dùng --drop).
    """
    if db.engine == "postgres":
        if fmt == "pgdump":
            script = ('PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -h 127.0.0.1 -U "$POSTGRES_USER" '
                      '-d "$POSTGRES_DB" --no-owner --no-privileges' + (" --clean --if-exists" if clean else ""))
        else:
            script = 'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q'
    elif db.engine in ("mysql", "mariadb"):
        client, p = ("mysql", "MYSQL") if db.engine == "mysql" else ("mariadb", "MARIADB")
        # Dùng root: dump thường có DEFINER/trigger mà user thường không tạo được
        script = f'MYSQL_PWD="${p}_ROOT_PASSWORD" {client} -h 127.0.0.1 -u root --force "${p}_DATABASE"'
    elif db.engine == "mongo" and fmt.startswith("mongo-json"):
        # Tên collection đã lọc chỉ còn [a-z0-9_.-] nên đặt thẳng vào lệnh shell được
        script = ('mongoimport --host 127.0.0.1 -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
                  f'--authenticationDatabase admin --db "$MONGO_INITDB_DATABASE" '
                  f'--collection {collection_name(filename)} --drop'
                  + (" --jsonArray" if fmt == "mongo-json-array" else ""))
    elif db.engine == "mongo":
        script = ('mongorestore --host 127.0.0.1 -u "$MONGO_INITDB_ROOT_USERNAME" -p "$MONGO_INITDB_ROOT_PASSWORD" '
                  '--authenticationDatabase admin --archive --drop')
    else:
        raise ValueError(t("db.import_unsupported", engine=ENGINES[db.engine].label))
    return ["sh", "-c", script]


def check_format(db: Database, fmt: str) -> None:
    allowed = {"postgres": {"sql", "pgdump"}, "mysql": {"sql"}, "mariadb": {"sql"}, "mongo": {"mongo-archive", "mongo-json", "mongo-json-array"}}
    if fmt not in allowed.get(db.engine, set()):
        hint = t("db.hint_mongo") if db.engine == "mongo" else t("db.hint_sql")
        raise ValueError(t("db.backup_format", engine=ENGINES[db.engine].label, hint=hint))


def read_backup(path: Path, gz: bool, engine: str) -> Iterator[bytes]:
    """Đọc file backup theo từng khối; với MySQL bỏ lệnh CREATE DATABASE/USE để nạp vào đúng database."""
    opener = gzip.open if gz else open
    with opener(path, "rb") as f:
        if engine not in ("mysql", "mariadb"):
            while chunk := f.read(CHUNK):
                yield chunk
            return
        buf: list[bytes] = []
        size = 0
        for line in f:
            head = line.lstrip()[:16].upper()
            if head.startswith((b"CREATE DATABASE", b"USE ")):
                continue
            buf.append(line)
            size += len(line)
            if size >= CHUNK:
                yield b"".join(buf)
                buf, size = [], 0
        if buf:
            yield b"".join(buf)


def wait_ready(docker, db: Database, timeout: float | None = None) -> bool:
    deadline = time.monotonic() + (timeout if timeout is not None else READY_TIMEOUT)
    while time.monotonic() < deadline:
        if docker.exec_ok(db.container, health_cmd(db)):
            return True
        time.sleep(min(2, max(0.0, deadline - time.monotonic())))
    return False
