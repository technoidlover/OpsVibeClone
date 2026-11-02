import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from . import __version__, i18n, legacy, panel_backup, panel_settings, project_backups, proxy, updates
from .assistant.session import Manager as AssistantManager
from .api import ai, assistant, auth, backup, browse, dashboard, databases, domains, files, github, jobs, projects, system, templates, webhooks
from .config import Settings, get_settings
from .crypto import SecretBox
from .db import init_engine, session_factory
from .docker import Docker
from .host import Sampler
from .backups import BackupScheduler
from .janitor import Janitor
from .jobs import JobQueue
from .pipelines import PIPELINES
from .web import mount_web


def _restart_panel() -> None:
    """Thoát tiến trình sau khi trả lời request; Docker (--restart unless-stopped) chạy lại panel."""
    logging.getLogger(__name__).warning("Khởi động lại panel để áp dụng bản sao lưu")
    threading.Timer(1.5, os._exit, (0,)).start()


def _bootstrap_settings(settings: Settings) -> None:
    """Ghi giá trị khởi tạo từ biến môi trường vào DB (chỉ khi DB chưa có)."""
    with session_factory()() as db:
        panel_settings.setdefault(db, panel_settings.PANEL_DOMAIN, settings.initial_panel_domain)
        panel_settings.setdefault(db, panel_settings.ACME_EMAIL, settings.initial_acme_email)
        if settings.public_ip:
            panel_settings.setdefault(db, panel_settings.BASE_DOMAIN, panel_settings.sslip_domain(settings.public_ip))
        db.commit()
        i18n.set_panel_lang(panel_settings.get(db, panel_settings.LANGUAGE))
        auth.ensure_setup_token(settings, db)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.ensure_dirs()
        legacy.migrate_files(settings)
        # Bản sao lưu toàn bộ vừa chuẩn bị khôi phục: thay DB / secret.key trước khi mở chúng
        panel_backup.apply_pending(settings)
        init_engine(f"sqlite:///{settings.db_path}")
        _bootstrap_settings(settings)
        docker = Docker(settings.network) if settings.docker_enabled else None
        if docker is not None:
            legacy.migrate_network(docker, settings)
        app.state.settings = settings
        app.state.docker = docker
        app.state.box = SecretBox.from_file(settings.secret_key_path)
        app.state.sessions = session_factory()
        databases.recover(app.state.sessions)
        project_backups.recover(settings)
        app.state.queue = JobQueue(
            settings, session_factory(), app.state.box, PIPELINES, docker
        )
        app.state.queue.recover()
        app.state.sampler = Sampler(settings.host_proc, settings.data_dir)
        app.state.sampler.start()
        app.state.assistant = AssistantManager(
            settings, session_factory(), app.state.box, docker, app.state.queue, app.state.sampler, i18n.current
        )
        app.state.assistant.recover()
        app.state.janitor = Janitor(docker) if docker is not None else None
        if app.state.janitor:
            app.state.janitor.start()
        app.state.restart_panel = _restart_panel
        app.state.updates = updates.Updates(docker, settings)
        app.state.updates.start()
        app.state.restorer = panel_backup.RestoreRunner(session_factory(), app.state.box, docker, settings, app.state.queue)
        app.state.restorer.start()
        app.state.backups = BackupScheduler(session_factory(), docker, settings, app.state.box) if docker is not None else None
        if app.state.backups:
            app.state.backups.start()
        if docker is not None:
            # Kéo image Traefik có thể mất vài giây: không chặn panel khởi động
            def start_traefik():
                legacy.migrate_acme(docker, proxy.ACME_VOLUME)
                system.refresh_traefik(docker, settings)

            threading.Thread(target=start_traefik, daemon=True).start()
        yield
        app.state.sampler.stop()
        app.state.updates.stop()
        if app.state.janitor:
            app.state.janitor.stop()
        if app.state.backups:
            app.state.backups.stop()
        app.state.assistant.shutdown()
        app.state.queue.shutdown()

    app = FastAPI(title="OpsVibes", version=__version__, lifespan=lifespan)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def request_language(request: Request, call_next):
        # Đặt trước call_next: endpoint (kể cả validator pydantic) chạy trong bản sao context này
        token = i18n.set_request_lang(i18n.parse_accept_language(request.headers.get("accept-language")))
        try:
            return await call_next(request)
        finally:
            i18n.reset_request_lang(token)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.get("/api/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    from .assistant import mcp as assistant_mcp
    for module in (auth, projects, files, browse, backup, databases, domains, templates, jobs, webhooks, system, dashboard, ai, assistant, github,
                   assistant_mcp):
        app.include_router(module.router)
    # Đăng ký sau cùng: route bắt mọi đường dẫn của giao diện
    mount_web(app, settings.web_dir)

    return app


class _QuietAccessLog(logging.Filter):
    """Bỏ khỏi access log: healthcheck (mỗi 10 giây) và lượt Traefik đọc route
    (mỗi 5 giây, URL chứa token)."""

    HIDDEN = ("/api/health", "/api/internal/")

    def filter(self, record: logging.LogRecord) -> bool:
        path = record.args[2] if isinstance(record.args, tuple) and len(record.args) >= 3 else ""
        return not str(path).startswith(self.HIDDEN)


def run() -> None:
    import copy

    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # Gắn filter qua log_config: uvicorn cấu hình lại logging khi khởi động
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["quiet"] = {"()": _QuietAccessLog}
    log_config["handlers"]["access"]["filters"] = ["quiet"]
    uvicorn.run(create_app(), host="0.0.0.0", port=3000, proxy_headers=False, log_config=log_config)


if __name__ == "__main__":
    run()
