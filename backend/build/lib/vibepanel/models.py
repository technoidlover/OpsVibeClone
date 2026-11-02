import json
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    """SQLite không lưu múi giờ: ghi dạng UTC naive, đọc ra luôn là UTC aware."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            if value.tzinfo is None:
                raise ValueError("Chỉ chấp nhận datetime có múi giờ")
            value = value.astimezone(timezone.utc).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        return value.replace(tzinfo=timezone.utc) if value is not None else None


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Chỉ lưu SHA-256 của token: lộ DB cũng không chiếm được phiên
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    user: Mapped[User] = relationship()


# Service dùng database nào: deploy chờ các database này sẵn sàng rồi mới chạy app
service_databases = Table(
    "service_databases",
    Base.metadata,
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
    Column("database_id", ForeignKey("databases.id", ondelete="CASCADE"), primary_key=True),
)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Sao lưu cả project tự động mỗi ngày, giữ backup_keep bản (xem project_backups.py)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    backup_keep: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    last_backup_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    services: Mapped[list["Service"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )
    databases: Mapped[list["Database"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", passive_deletes=True
    )


class Service(Base):
    __tablename__ = "services"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    repo_url: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str] = mapped_column(String(255), default="main")
    domain: Mapped[str | None] = mapped_column(String(253), unique=True, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=True)
    # Lệnh khởi động cho Nixpacks; None = để Nixpacks/panel tự nhận diện
    start_command: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Model AI tự sửa cấu hình khi deploy lỗi ("provider:<id>", xem assistant/heal.py); None = không dùng AI
    deploy_agent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Job đang phục vụ traffic (không đặt FK để tránh phụ thuộc vòng services <-> jobs)
    active_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Các trường *_enc được mã hóa bằng Fernet (xem crypto.py)
    git_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret_enc: Mapped[str] = mapped_column(Text)
    env_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Repo chọn qua GitHub App: clone bằng token cài đặt, webhook đi qua App
    github_installation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    github_repo_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    github_repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Nguồn mã: "git" (repo), "image" (image Docker có sẵn), "archive" (file nén tải lên),
    # "manual" (chỉ có file riêng người dùng tự soạn: Dockerfile / compose.yml / .sql...)
    source: Mapped[str] = mapped_column(String(20), default="git")
    # "auto": Dockerfile nếu có, không thì Nixpacks; "compose": chạy compose.yml bằng docker compose
    build_mode: Mapped[str] = mapped_column(String(20), default="auto", server_default="auto")
    # Sao lưu dữ liệu (volume) tự động mỗi ngày, giữ backup_keep bản (xem service_backups.py)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    backup_keep: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    last_backup_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Kiểm tra bản mới đã lên: "tcp" (cổng mở), "http" (health_path trả 2xx/3xx), "none" (worker, không cổng)
    health_check: Mapped[str] = mapped_column(String(10), default="tcp", server_default="tcp")
    health_path: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Service trong compose.yml nhận traffic từ tên miền; None = tự chọn
    compose_service: Mapped[str | None] = mapped_column(String(63), nullable=True)
    # Thư mục con trong repo chứa app (monorepo); None = thư mục gốc
    root_dir: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Tên file nén đang dùng (service nguồn archive)
    archive_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Volume chứa mã nguồn website PHP tải lên bằng file nén, gắn vào /app (xem site_files.py)
    site_volume: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Service chạy thẳng image Docker (ứng dụng 1-click hoặc image tự nhập), không build từ repo
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Khóa trong templates.TEMPLATES nếu tạo từ ứng dụng 1-click
    template: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # JSON: danh sách thư mục trong container được giữ bằng Docker volume
    volumes_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    project: Mapped[Project] = relationship(back_populates="services")
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="service", cascade="all, delete-orphan", passive_deletes=True
    )
    databases: Mapped[list["Database"]] = relationship(secondary=service_databases, passive_deletes=True)

    @property
    def slug(self) -> str:
        """Tên duy nhất trên máy chủ, dùng cho thư mục workspace, image và container."""
        return f"{self.project.name}_{self.name}"

    @property
    def internal_host(self) -> str:
        """Tên host nội bộ ổn định (network alias) để service khác gọi tới, như host của database."""
        alias = f"{self.project.name}-{self.name}"
        return alias if len(alias) <= 63 else f"svc-{self.id}-{self.name}"[:63]

    @property
    def volumes(self) -> list[str]:
        return json.loads(self.volumes_json) if self.volumes_json else []

    @volumes.setter
    def volumes(self, mounts: list[str]) -> None:
        self.volumes_json = json.dumps(mounts) if mounts else None


class JobKind(StrEnum):
    DEPLOY = "deploy"
    ROLLBACK = "rollback"
    # Mang patch do AI Agent tạo (deploy_service apply_changes); không tự chạy
    AGENT = "agent"
    # Khôi phục dữ liệu (volume) từ bản sao lưu; job.prompt = tên bản sao lưu
    RESTORE = "restore"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobTrigger(StrEnum):
    MANUAL = "manual"
    WEBHOOK = "webhook"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default=JobKind.DEPLOY)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED, index=True)
    trigger: Mapped[str] = mapped_column(String(20), default=JobTrigger.MANUAL)
    branch: Mapped[str] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Dòng đầu của commit message (hoặc tên file nén / image) để hiện như Vercel
    commit_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image: Mapped[str | None] = mapped_column(String(255), nullable=True)
    container: Mapped[str | None] = mapped_column(String(255), nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Với job rollback: job cũ có image được chạy lại
    source_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Với job deploy: áp patch của job agent này lên mã nguồn trước khi build
    patch_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Job deploy: model AI ("provider:<id>") tự sửa cấu hình nếu build/khởi động lỗi
    heal_agent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # AI Agent đọc mã nguồn, thêm cấu hình / biến môi trường còn thiếu trước khi build
    ai_prepare: Mapped[bool] = mapped_column(Boolean, default=False)
    # Cuộc hội thoại AI Agent ghi lại lần tự sửa của job deploy này
    assistant_session_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Job agent: cho phép sửa cả mã nguồn (mặc định chỉ file cấu hình)
    allow_code: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Job agent (patch của AI Agent) và job deploy có tự sửa: tóm tắt của AI và diff cấu hình
    agent: Mapped[str | None] = mapped_column(String(20), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    diff: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    service: Mapped[Service] = relationship(back_populates="jobs")


class Setting(Base):
    """Cấu hình panel chỉnh được lúc chạy (tên miền panel, email ACME, ...)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class AIProvider(Base):
    """API key của một nhà cung cấp AI (Anthropic, OpenAI, Gemini, ...)."""

    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Khóa trong ai.PROVIDERS
    kind: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(60), unique=True)
    base_url: Mapped[str] = mapped_column(String(500))
    api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class GitHubApp(Base):
    """GitHub App mà panel tự tạo qua manifest (mỗi panel một App, id luôn = 1)."""

    __tablename__ = "github_app"

    id: Mapped[int] = mapped_column(primary_key=True)
    app_id: Mapped[int] = mapped_column(Integer)
    slug: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    owner_login: Mapped[str | None] = mapped_column(String(100), nullable=True)
    html_url: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_secret_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_secret_enc: Mapped[str] = mapped_column(Text)
    private_key_enc: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Database(Base):
    """Database 1-click của một project (container + volume riêng, xem databases.py)."""

    __tablename__ = "databases"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(40))
    # Khóa trong databases.ENGINES và tag image
    engine: Mapped[str] = mapped_column(String(20))
    version: Mapped[str] = mapped_column(String(20))
    db_name: Mapped[str] = mapped_column(String(64))
    username: Mapped[str] = mapped_column(String(64))
    password_enc: Mapped[str] = mapped_column(Text)
    root_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Lần import backup gần nhất: running | success | failed
    import_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Lỗi gần nhất khi tạo container hoặc import backup
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sao lưu tự động mỗi ngày, giữ backup_keep bản gần nhất (xem backups.py)
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    backup_keep: Mapped[int] = mapped_column(Integer, default=7, server_default="7")
    last_backup_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    project: Mapped[Project] = relationship(back_populates="databases")

    @property
    def slug(self) -> str:
        return f"{self.project.name}_{self.name}"

    @property
    def container(self) -> str:
        return f"vp_db_{self.slug}"

    @property
    def volume(self) -> str:
        return f"vp_db_{self.slug}"

    @property
    def host(self) -> str:
        """Tên host nội bộ app dùng để kết nối (một nhãn DNS tối đa 63 ký tự)."""
        alias = f"{self.project.name}-{self.name}"
        return alias if len(alias) <= 63 else f"db-{self.id}-{self.name}"[:63]


class AssistantSession(Base):
    """Một cuộc hội thoại với AI Agent (cả máy chủ, không gắn riêng service nào)."""

    __tablename__ = "assistant_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    # "provider:<id>" (model qua API) hoặc "cli:<tool>" (agy/claude đăng nhập tài khoản)
    brain: Mapped[str] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # idle | running | waiting (chờ duyệt thao tác) | error
    status: Mapped[str] = mapped_column(String(20), default="idle")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON: ngữ cảnh khi mở từ nút "Nhờ AI sửa" ({"project", "service", "job_id"})
    context_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON: các tool call của lượt hiện tại, kết quả đã có và quyết định duyệt của người dùng
    pending_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    messages: Mapped[list["AssistantMessage"]] = relationship(
        back_populates="session", cascade="all, delete-orphan", passive_deletes=True,
        order_by="AssistantMessage.id",
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("assistant_sessions.id", ondelete="CASCADE"), index=True)
    # "user" (gồm cả kết quả tool) hoặc "assistant"
    role: Mapped[str] = mapped_column(String(20))
    # JSON: danh sách block theo định dạng chung ở assistant/llm.py
    content_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    session: Mapped[AssistantSession] = relationship(back_populates="messages")

    @property
    def content(self) -> list[dict]:
        return json.loads(self.content_json)
