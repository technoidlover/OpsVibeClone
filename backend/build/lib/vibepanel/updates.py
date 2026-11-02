"""Bản mới của panel: kiểm tra trên registry và tự cập nhật bằng một container phụ.

- Kiểm tra: đọc tag dạng X.Y.Z của image panel trên registry (chuẩn Docker Registry v2,
  token ẩn danh nên chạy được với GHCR / Docker Hub khi image công khai).
- Cập nhật: panel kéo image mới (lỗi mạng thì panel vẫn chạy bản cũ), rồi chạy container
  `opsvibes-updater` từ image mới. Container đó thay container panel bằng bản mới với cùng
  cấu hình, chờ bản mới healthy; không lên được thì trả lại bản cũ. Panel không tự làm được
  vì bước thay container sẽ dừng chính nó.
- Trạng thái nằm trong file update-status.json ở thư mục dữ liệu (updater cũng mount thư mục này).
"""

import argparse
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Settings
from .docker import MANAGED_LABEL, Docker, DockerError, create_body

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 6 * 3600
HEALTH_TIMEOUT_SECONDS = 180
UPDATER_CONTAINER = "opsvibes-updater"
STATUS_FILE = "update-status.json"
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


def parse_version(tag: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match(tag or "")
    return tuple(int(x) for x in m.groups()) if m else None


def newest(tags: list[str]) -> str | None:
    versions = [(v, tag) for tag in tags if (v := parse_version(tag))]
    return max(versions)[1].lstrip("v") if versions else None


def is_newer(candidate: str | None, current: str) -> bool:
    a, b = parse_version(candidate or ""), parse_version(current)
    return bool(a and b and a > b)


def split_image(ref: str) -> tuple[str, str | None]:
    """'ghcr.io/opsvibes/opsvibes:0.31.1' -> ('ghcr.io/opsvibes/opsvibes', '0.31.1')."""
    ref = ref.split("@", 1)[0]
    head, _, last = ref.rpartition("/")
    if ":" in last:
        name, tag = last.split(":", 1)
        return (f"{head}/{name}" if head else name), tag
    return ref, None


# ---------- Registry ----------

def _get_json(url: str, token: str | None = None, timeout: float = 15) -> dict:
    headers = {"Accept": "application/json", "User-Agent": f"OpsVibes/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as res:
        return json.loads(res.read().decode())


def registry_tags(repository: str, timeout: float = 15) -> list[str]:
    host, _, path = repository.partition("/")
    if not path or ("." not in host and ":" not in host and host != "localhost"):
        # Docker Hub: "user/app" hoặc image chính thức "app"
        host, path = "registry-1.docker.io", repository if "/" in repository else f"library/{repository}"
    url = f"https://{host}/v2/{path}/tags/list?n=1000"
    try:
        return _get_json(url, timeout=timeout).get("tags") or []
    except urllib.error.HTTPError as e:
        challenge = e.headers.get("WWW-Authenticate", "")
        if e.code != 401 or not challenge.lower().startswith("bearer"):
            raise
    params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
    realm = params.pop("realm", "")
    data = _get_json(f"{realm}?{urllib.parse.urlencode(params)}", timeout=timeout)
    token = data.get("token") or data.get("access_token")
    return _get_json(url, token, timeout=timeout).get("tags") or []


# ---------- Trạng thái ----------

def status_path(settings: Settings) -> Path:
    return settings.data_dir / STATUS_FILE


def write_status(path: Path, state: str, **fields) -> None:
    data = {"state": state, "at": datetime.now(timezone.utc).isoformat(), **fields}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def read_status(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


class Updates:
    """Kiểm tra định kỳ + bắt đầu cập nhật. Một instance cho cả app (app.state.updates)."""

    def __init__(self, docker: Docker | None, settings: Settings):
        self.docker, self.settings = docker, settings
        self.latest: str | None = None
        self.checked_at: str | None = None
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ----- Image của chính panel -----

    def current_image(self) -> str | None:
        if self.docker is None:
            return None
        try:
            return (self.docker.inspect(self.settings.panel_container).get("Config") or {}).get("Image")
        except DockerError:
            return None

    def repository(self) -> str | None:
        override = os.environ.get("OPSVIBES_UPDATE_REPOSITORY")
        if override:
            return override
        image = self.current_image()
        return split_image(image)[0] if image else None

    # ----- Kiểm tra -----

    def check(self) -> None:
        repository = self.repository()
        with self._lock:
            self.checked_at = datetime.now(timezone.utc).isoformat()
            if not repository:
                self.error = "no_image"
                return
            try:
                self.latest = newest(registry_tags(repository))
                self.error = None
            except (OSError, ValueError, urllib.error.URLError) as e:
                # Image private / mất mạng: lần sau thử lại, không làm phiền người dùng
                logger.info("Không kiểm tra được bản mới (%s): %s", repository, e)
                self.error = "unreachable"

    def _loop(self) -> None:
        while not self._stop.wait(5):
            self.check()
            if self._stop.wait(CHECK_INTERVAL_SECONDS):
                return

    def start(self) -> None:
        self._finish_previous()
        if not self.settings.update_check:
            return
        self._thread = threading.Thread(target=self._loop, name="opsvibes-updates", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _finish_previous(self) -> None:
        """Panel vừa khởi động: chốt trạng thái của lần cập nhật trước (nếu updater chưa kịp ghi)."""
        path = status_path(self.settings)
        status = read_status(path)
        if not status or status.get("state") not in ("pulling", "restarting"):
            return
        if status.get("to") == __version__:
            write_status(path, "done", **{k: status.get(k) for k in ("from", "to")})
        elif self.docker is None or not self.docker.container_exists(UPDATER_CONTAINER):
            write_status(path, "failed", error="interrupted", **{k: status.get(k) for k in ("from", "to")})

    def info(self) -> dict:
        with self._lock:
            latest, checked_at, error = self.latest, self.checked_at, self.error
        return {
            "current": __version__,
            "latest": latest,
            "update_available": is_newer(latest, __version__),
            "checked_at": checked_at,
            "error": error,
            "can_update": self.docker is not None and self.current_image() is not None,
            "status": read_status(status_path(self.settings)),
        }

    # ----- Cập nhật -----

    def begin(self, version: str) -> None:
        """Kéo image rồi giao cho updater; chạy nền, tiến độ ghi vào update-status.json."""
        if not is_newer(version, __version__):
            raise ValueError("not_newer")
        if self.docker is None:
            raise RuntimeError("no_docker")
        status = read_status(status_path(self.settings))
        if status and status.get("state") in ("pulling", "restarting") and self.docker.container_exists(UPDATER_CONTAINER):
            raise RuntimeError("in_progress")
        repository = self.repository()
        if not repository:
            raise RuntimeError("no_image")
        image = f"{repository}:{version}"
        path = status_path(self.settings)
        write_status(path, "pulling", **{"from": __version__, "to": version})
        threading.Thread(target=self._run, args=(image, version, path), name="opsvibes-update", daemon=True).start()

    def _run(self, image: str, version: str, path: Path) -> None:
        versions = {"from": __version__, "to": version}
        try:
            self.docker.pull_image(image)
            write_status(path, "restarting", **versions)
            self.docker.remove_container(UPDATER_CONTAINER)
            self.docker.run_raw(self._updater_args(image, path))
            # Cập nhật thành công thì tiến trình này bị thay trước khi vòng lặp kết thúc. Còn chạy
            # tới đây mà updater đã biến mất (--rm) với trạng thái chưa chốt: updater hỏng giữa chừng.
            deadline = time.monotonic() + 15 * 60
            while time.monotonic() < deadline and self.docker.container_exists(UPDATER_CONTAINER):
                time.sleep(5)
            if (read_status(path) or {}).get("state") == "restarting":
                write_status(path, "failed", error="updater exited", **versions)
        except DockerError as e:
            logger.error("Cập nhật lên %s thất bại: %s", version, e)
            write_status(path, "failed", error=str(e)[-500:], **versions)

    def _updater_args(self, image: str, path: Path) -> list[str]:
        info = self.docker.inspect(self.settings.panel_container)
        mounts = {m.get("Destination"): m.get("Source") for m in info.get("Mounts") or []}
        socket = mounts.get("/var/run/docker.sock", "/var/run/docker.sock")
        data = mounts.get(str(self.settings.data_dir), str(self.settings.data_dir))
        security = [arg for opt in (info.get("HostConfig") or {}).get("SecurityOpt") or [] for arg in ("--security-opt", opt)]
        return [
            "-d", "--rm", "--name", UPDATER_CONTAINER, "--label", f"{MANAGED_LABEL}=true",
            "-v", f"{socket}:/var/run/docker.sock",
            "-v", f"{data}:{self.settings.data_dir}",
            *security,
            "--entrypoint", "opsvibes-update",
            image,
            "--container", self.settings.panel_container, "--image", image, "--status", str(path),
        ]


# ---------- Updater (chạy trong container opsvibes-updater, từ image mới) ----------

# Image không có HEALTHCHECK: phải chạy liên tục chừng này giây mới coi là lên được
STABLE_SECONDS = 15


def _wait_healthy(docker: Docker, name: str, timeout: float) -> None:
    """Chờ container mới báo healthy. Thoát / unhealthy / bị Docker khởi động lại / quá giờ đều
    coi là lỗi (để quay lại bản cũ). Container kế thừa --restart unless-stopped của panel nên
    bản lỗi thoát ra sẽ được Docker chạy lại liên tục: phải xét cả số lần restart."""
    deadline = time.monotonic() + timeout
    first_start = None
    running_since = None
    while time.monotonic() < deadline:
        data = docker.inspect(name)
        state = data.get("State") or {}
        started = state.get("StartedAt")
        first_start = first_start or started
        if state.get("Restarting") or data.get("RestartCount") or started != first_start:
            raise DockerError(f"container keeps restarting (exit {state.get('ExitCode')})")
        if not state.get("Running"):
            raise DockerError(f"container {state.get('Status') or 'stopped'} (exit {state.get('ExitCode')})")
        health = (state.get("Health") or {}).get("Status")
        if health == "healthy":
            return
        if health == "unhealthy":
            raise DockerError("container unhealthy")
        if health is None:
            running_since = running_since or time.monotonic()
            if time.monotonic() - running_since >= STABLE_SECONDS:
                return
        time.sleep(2)
    raise DockerError(f"not healthy after {int(timeout)}s")


def run_update(docker: Docker, name: str, image: str, path: Path, health_timeout: float = HEALTH_TIMEOUT_SECONDS) -> bool:
    info = docker.inspect(name)
    previous = read_status(path) or {}
    versions = {"from": previous.get("from"), "to": previous.get("to")}
    body = create_body(info, (info.get("HostConfig") or {}).get("NetworkMode") or docker.network, image=image,
                       old_image=docker.image_config(info.get("Image") or ""))
    backup = f"{name}-previous"
    docker.remove_container(backup)
    docker.stop(name)
    docker.rename(name, backup)
    try:
        docker.create(name, body)
        docker.start(name)
        _wait_healthy(docker, name, health_timeout)
    except DockerError as e:
        logs = ""
        try:
            logs = docker.logs(name, 30)
        except DockerError:
            pass
        docker.remove_container(name)
        docker.rename(backup, name)
        docker.start(name)
        write_status(path, "failed", error=f"{e}\n{logs}"[-1500:], **versions)
        return False
    docker.remove_container(backup)
    write_status(path, "done", **versions)
    return True


def updater_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsvibes-update", description="Thay container panel bằng image mới")
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--status", required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    docker = Docker(network="")
    try:
        ok = run_update(docker, args.container, args.image, Path(args.status))
    except DockerError as e:
        write_status(Path(args.status), "failed", error=str(e)[-1500:])
        return 1
    return 0 if ok else 1
