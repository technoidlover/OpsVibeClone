from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None

# Mỗi phiên bản schema mới thêm một mục. DB tạo mới dùng thẳng create_all và
# nhảy lên SCHEMA_VERSION; DB cũ chạy lần lượt các câu lệnh còn thiếu.
_MIGRATIONS: dict[int, list[str]] = {
    2: [
        "ALTER TABLE services ADD COLUMN active_job_id INTEGER",
        "ALTER TABLE jobs ADD COLUMN image VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN container VARCHAR(255)",
        "ALTER TABLE jobs ADD COLUMN port INTEGER",
        "ALTER TABLE jobs ADD COLUMN source_job_id INTEGER",
    ],
    3: [
        "ALTER TABLE jobs ADD COLUMN agent VARCHAR(20)",
        "ALTER TABLE jobs ADD COLUMN prompt TEXT",
        "ALTER TABLE jobs ADD COLUMN result TEXT",
        "ALTER TABLE jobs ADD COLUMN diff TEXT",
    ],
    4: [
        "ALTER TABLE services ADD COLUMN github_installation_id INTEGER",
        "ALTER TABLE services ADD COLUMN github_repo_id INTEGER",
        "ALTER TABLE services ADD COLUMN github_repo VARCHAR(255)",
        "CREATE INDEX IF NOT EXISTS ix_services_github_repo_id ON services (github_repo_id)",
    ],
    5: ["ALTER TABLE services ADD COLUMN start_command VARCHAR(500)"],
    6: ["ALTER TABLE jobs ADD COLUMN patch_job_id INTEGER"],
    7: [
        "ALTER TABLE services ADD COLUMN deploy_agent VARCHAR(20)",
        "ALTER TABLE jobs ADD COLUMN heal_agent VARCHAR(20)",
        "ALTER TABLE jobs ADD COLUMN allow_code BOOLEAN NOT NULL DEFAULT 0",
    ],
    8: [
        "ALTER TABLE services ADD COLUMN image VARCHAR(255)",
        "ALTER TABLE services ADD COLUMN template VARCHAR(40)",
        "ALTER TABLE services ADD COLUMN volumes_json TEXT",
    ],
    9: [
        "ALTER TABLE services ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'git'",
        "ALTER TABLE services ADD COLUMN archive_name VARCHAR(255)",
        "UPDATE services SET source = 'image' WHERE image IS NOT NULL",
    ],
    10: ["ALTER TABLE services ADD COLUMN root_dir VARCHAR(255)"],
    11: ["ALTER TABLE jobs ADD COLUMN commit_message VARCHAR(300)"],
    # Bỏ agy và đăng nhập tài khoản: CLI agent chỉ còn Claude Code chạy bằng API key
    12: [
        "UPDATE services SET deploy_agent = NULL WHERE deploy_agent = 'agy'",
        "DELETE FROM agent_tools WHERE tool = 'agy'",
        "UPDATE agent_tools SET auth_mode = 'api_key', token_enc = NULL",
    ],
    # Tự sửa khi deploy chuyển từ Claude Code (CLI) sang AI Agent: giữ đúng provider Anthropic đã chọn
    13: [
        "ALTER TABLE jobs ADD COLUMN assistant_session_id INTEGER",
        "UPDATE services SET deploy_agent = (SELECT 'provider:' || provider_id FROM agent_tools "
        "WHERE tool = 'claude' AND enabled = 1 AND provider_id IS NOT NULL) WHERE deploy_agent = 'claude'",
        "DROP TABLE IF EXISTS agent_tools",
    ],
    # AI Agent chuẩn bị cấu hình trước khi build
    14: ["ALTER TABLE jobs ADD COLUMN ai_prepare BOOLEAN NOT NULL DEFAULT 0"],
    # Tự viết Dockerfile / compose.yml
    15: [
        "ALTER TABLE services ADD COLUMN build_mode VARCHAR(20) NOT NULL DEFAULT 'auto'",
        "ALTER TABLE services ADD COLUMN compose_service VARCHAR(63)",
    ],
    # Kiểm tra sức khỏe (HTTP / worker) và sao lưu database
    16: [
        "ALTER TABLE services ADD COLUMN health_check VARCHAR(10) NOT NULL DEFAULT 'tcp'",
        "ALTER TABLE services ADD COLUMN health_path VARCHAR(200)",
        "ALTER TABLE databases ADD COLUMN backup_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE databases ADD COLUMN backup_keep INTEGER NOT NULL DEFAULT 7",
        "ALTER TABLE databases ADD COLUMN last_backup_at DATETIME",
    ],
    # Sao lưu dữ liệu service
    17: [
        "ALTER TABLE services ADD COLUMN backup_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE services ADD COLUMN backup_keep INTEGER NOT NULL DEFAULT 7",
        "ALTER TABLE services ADD COLUMN last_backup_at DATETIME",
    ],
    # Sao lưu theo project, volume mã nguồn website PHP
    18: [
        "ALTER TABLE services ADD COLUMN site_volume VARCHAR(255)",
        "ALTER TABLE projects ADD COLUMN backup_enabled BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE projects ADD COLUMN backup_keep INTEGER NOT NULL DEFAULT 7",
        "ALTER TABLE projects ADD COLUMN last_backup_at DATETIME",
    ],
}
SCHEMA_VERSION = max(_MIGRATIONS)


def _migrate(conn: Connection) -> None:
    is_new = not inspect(conn).has_table("users")
    if is_new:
        Base.metadata.create_all(conn)
    else:
        # Bản 0.1.0 chưa đặt user_version: coi là schema 1
        version = conn.exec_driver_sql("PRAGMA user_version").scalar() or 1
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(target, []):
                try:
                    conn.exec_driver_sql(statement)
                except OperationalError as e:
                    # Bảng chưa có (DB rất cũ): create_all bên dưới tạo nó với đủ cột, không có gì để sửa
                    if not (statement.startswith(("UPDATE", "DELETE", "ALTER")) and "no such table" in str(e)):
                        raise
        # Tạo các bảng mới hoàn toàn (vd settings)
        Base.metadata.create_all(conn)
    conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


def init_engine(url: str) -> Engine:
    global _engine, _SessionLocal
    _engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        # WAL cho phép worker build ghi trong lúc API vẫn đọc
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)

    from . import models  # noqa: F401  (đăng ký bảng vào metadata)

    with _engine.begin() as conn:
        _migrate(conn)
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        raise RuntimeError("Database chưa được khởi tạo")
    return _SessionLocal


def get_db() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()
