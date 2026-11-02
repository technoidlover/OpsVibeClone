"""Cấu hình tập trung, đọc từ biến môi trường tiền tố OPSVIBES_.

Mọi đường dẫn đều nằm dưới `data_dir` (mặc định /etc/opsvibes khi chạy trong
container), nên không còn đường dẫn hay tên miền hard-code rải rác trong code.

Sản phẩm từng tên VibePanel: biến VIBEPANEL_* vẫn được đọc khi chưa đặt tên mới,
và các tên LEGACY_* bên dưới dùng để chuyển dữ liệu của bản cài cũ (xem legacy.py).
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(f"OPSVIBES_{name}", os.environ.get(f"VIBEPANEL_{name}", default))


def _env_list(name: str) -> list[str]:
    raw = _env(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/etc/opsvibes")))
    # Bật khi panel chạy sau HTTPS để cookie phiên chỉ gửi qua kết nối mã hóa
    secure_cookies: bool = field(default_factory=lambda: _env("SECURE_COOKIES", "false").lower() == "true")
    session_ttl_hours: int = field(default_factory=lambda: int(_env("SESSION_TTL_HOURS", "168")))
    max_concurrent_builds: int = field(default_factory=lambda: int(_env("MAX_CONCURRENT_BUILDS", "2")))
    build_timeout_seconds: int = field(default_factory=lambda: int(_env("BUILD_TIMEOUT_SECONDS", "1800")))
    # Chỉ cần khi chạy frontend dev server ở origin khác (vd http://localhost:5173)
    cors_origins: list[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS"))
    # Bật khi panel có quyền điều khiển Docker (image chính thức đặt sẵn true)
    docker_enabled: bool = field(default_factory=lambda: _env("DOCKER", "false").lower() == "true")
    # Giá trị khởi tạo, chỉ áp dụng khi trong DB chưa có (sau đó chỉnh qua API)
    public_ip: str = field(default_factory=lambda: _env("PUBLIC_IP", ""))
    # /proc của host (image mount vào /host/proc) để đo cả lưu lượng mạng của server
    host_proc: Path = field(default_factory=lambda: Path(_env("HOST_PROC", "/proc")))
    initial_panel_domain: str = field(default_factory=lambda: _env("PANEL_DOMAIN", ""))
    initial_acme_email: str = field(default_factory=lambda: _env("ACME_EMAIL", ""))
    # Tự kiểm tra bản mới trên registry (tắt trong test hoặc máy không ra Internet)
    update_check: bool = field(default_factory=lambda: _env("UPDATE_CHECK", "true").lower() == "true")
    traefik_image: str = field(default_factory=lambda: _env("TRAEFIK_IMAGE", "traefik:v3.7.13"))
    # Bản build của frontend; image chính thức đặt sẵn đường dẫn này
    web_dir: Path = field(default_factory=lambda: Path(_env("WEB_DIR", str(Path(__file__).parents[2] / "frontend" / "dist"))))
    # Tên container và network mà install script/CI tạo cho panel
    network: str = "opsvibes"
    panel_container: str = "opsvibes"
    traefik_container: str = "opsvibes-traefik"
    keep_images: int = 5
    # Traefik poll mỗi 5 giây: chờ thêm chút trước khi gỡ container cũ
    route_switch_grace_seconds: float = 7.0

    @property
    def db_path(self) -> Path:
        return self.data_dir / "opsvibes.db"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def setup_token_path(self) -> Path:
        return self.data_dir / "setup.token"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def job_logs_dir(self) -> Path:
        return self.data_dir / "logs" / "jobs"

    def archive_path(self, service_id: int) -> Path:
        """File nén đang dùng của service nguồn archive (mỗi service giữ một bản)."""
        return self.data_dir / "archives" / f"{service_id}.bin"

    def files_dir(self, service_id: int) -> Path:
        """File riêng của service (Dockerfile, compose.yml, .sql...), chép vào mã nguồn mỗi lần deploy."""
        return self.data_dir / "files" / str(service_id)

    def compose_snapshot(self, service_id: int, job_id: int) -> Path:
        """Cấu hình compose đã giải (image gắn theo job) của một lần deploy, để rollback / khôi phục."""
        return self.data_dir / "compose" / str(service_id) / f"{job_id}.json"

    def patch_path(self, job_id: int) -> Path:
        """Patch đầy đủ (git diff --binary) mà một job agent tạo ra."""
        return self.job_logs_dir / f"{job_id}.patch"

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.workspaces_dir, self.job_logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        # DB chứa hash mật khẩu và secret: chỉ tiến trình panel được đọc
        self.data_dir.chmod(0o700)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Tên của bản cài trước khi đổi thương hiệu, chỉ dùng để chuyển sang tên mới
LEGACY_NAME = "vibepanel"
LEGACY_DB_FILE = "vibepanel.db"
LEGACY_TRAEFIK_CONTAINER = "vibepanel-traefik"
