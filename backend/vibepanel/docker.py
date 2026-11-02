"""Mọi thao tác với Docker Engine đều đi qua lớp này (gọi docker CLI, không qua shell).

Tách riêng để test thay được bằng bản giả, và để sau này đổi sang Docker API
mà không đụng tới pipeline.
"""

import gzip
import http.client
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from . import runner
from .i18n import t
from .runner import JobLogger

MANAGED_LABEL = "opsvibes.managed"
SERVICE_LABEL = "opsvibes.service"
JOB_LABEL = "opsvibes.job"
# Nhãn của container tạo trước khi đổi tên sản phẩm: vẫn đọc cho tới khi container được tạo lại
LEGACY_MANAGED_LABEL = "vibepanel.managed"
LEGACY_SERVICE_LABEL = "vibepanel.service"


def service_id_label(labels: dict[str, str]) -> str | None:
    return labels.get(SERVICE_LABEL) or labels.get(LEGACY_SERVICE_LABEL)


class DockerError(RuntimeError):
    pass


def _log_time(line: str) -> str:
    """Khóa sắp xếp từ timestamp RFC3339Nano đầu dòng; docker bỏ số 0 cuối phần lẻ giây nên phải bù cho đủ 9 số."""
    stamp = line.split(" ", 1)[0]
    if "." in stamp and stamp.endswith("Z"):
        head, frac = stamp[:-1].split(".", 1)
        return f"{head}.{frac.ljust(9, '0')}Z"
    return stamp


_SIZE_RE = re.compile(r"([\d.]+)\s*([kKMGTP]?i?B)")


def parse_size(text: str) -> int:
    """'12.3kB' / '1.2GB' / '512MiB' -> byte (docker in kích thước theo hệ 1000, có khi theo 1024)."""
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0
    unit = m.group(2)
    power = 0 if unit[0] == "B" else " KMGTP".index(unit[0].upper())
    return int(float(m.group(1)) * (1024 if "i" in unit else 1000) ** power)


def _query(args: list[str], timeout: float = 30) -> str:
    """Lệnh đọc nhanh, không cần ghi log job."""
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DockerError(t("docker.call_failed", command=args[0], error=e)) from e
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or t("docker.command_failed", command=args[0]))
    return result.stdout


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def _api(method: str, path: str, body: dict | None = None, timeout: float = 60) -> dict:
    """Gọi thẳng Docker Engine API qua docker.sock, cho các thao tác docker CLI không làm được."""
    conn = _UnixConnection(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"), timeout)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers={"Content-Type": "application/json"})
        res = conn.getresponse()
        raw = res.read().decode(errors="replace")
    except OSError as e:
        raise DockerError(t("docker.call_failed", command=path, error=e)) from e
    finally:
        conn.close()
    if res.status >= 400:
        try:
            message = json.loads(raw).get("message") or raw
        except ValueError:
            message = raw
        raise DockerError(message.strip() or f"HTTP {res.status}")
    return json.loads(raw) if raw.strip() else {}


# Cấu hình mà image tự đặt sẵn: khi đổi image, giá trị bằng mặc định của image cũ thì lấy theo image mới
_IMAGE_DEFAULTS = ("Cmd", "Entrypoint", "WorkingDir", "Healthcheck", "ExposedPorts", "Volumes", "User",
                   "StopSignal", "Shell", "OnBuild", "ArgsEscaped")


def create_body(info: dict, network: str, image: str | None = None, old_image: dict | None = None) -> dict:
    """Body cho POST /containers/create để tạo lại container `info` (kết quả docker inspect) y hệt,
    chạy trên `network`, và tùy chọn với `image` mới (`old_image`: Config của image đang dùng)."""
    name = info.get("Name", "").lstrip("/")
    host = dict(info.get("HostConfig") or {})
    old_network = host.get("NetworkMode")
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    skip = {name, (info.get("Id") or "")[:12]}
    aliases = [a for a in (networks.get(network) or networks.get(old_network) or {}).get("Aliases") or [] if a not in skip]

    # Volume (kể cả volume ẩn danh) gắn lại theo tên để không tạo volume trống mới
    binds = []
    for m in info.get("Mounts") or []:
        source = m.get("Name") if m.get("Type") == "volume" else m.get("Source") if m.get("Type") == "bind" else None
        if source:
            binds.append(f"{source}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro"))
    host.update(NetworkMode=network, Binds=binds or None, Mounts=None)

    config = {k: v for k, v in (info.get("Config") or {}).items() if k not in ("Hostname", "MacAddress")}
    if image:
        config["Image"] = image
        base = old_image or {}
        config["Env"] = [e for e in config.get("Env") or [] if e not in (base.get("Env") or [])]
        config["Labels"] = {k: v for k, v in (config.get("Labels") or {}).items() if (base.get("Labels") or {}).get(k) != v}
        for key in _IMAGE_DEFAULTS:
            if key in config and config[key] == base.get(key):
                del config[key]
    return {**config, "HostConfig": host, "NetworkingConfig": {"EndpointsConfig": {network: {"Aliases": aliases}}}}


def _capture(cmd: list[str], timeout: float) -> tuple[int, str]:
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 124, partial + "\n" + t("docker.exec_timeout", timeout=int(timeout))
    except OSError as e:
        raise DockerError(t("docker.call_failed", command=cmd[1], error=e)) from e
    return result.returncode, result.stdout + result.stderr


class Docker:
    def __init__(self, network: str):
        self.network = network

    # ---------- Network ----------

    def ensure_network(self) -> None:
        try:
            _query(["network", "inspect", self.network])
        except DockerError:
            _query(["network", "create", self.network])

    def containers_on_network_mode(self, network: str) -> list[str]:
        """Container (kể cả đã dừng) được tạo với --network `network`."""
        ids = _query(["ps", "-aq"]).split()
        if not ids:
            return []
        return [d.get("Name", "").lstrip("/") for d in json.loads(_query(["container", "inspect", *ids], timeout=120))
                if (d.get("HostConfig") or {}).get("NetworkMode") == network]

    def recreate_on_network(self, name: str) -> None:
        """Tạo lại container y hệt (image, env, volume, cổng, nhãn, restart policy, alias) nhưng
        chạy trên network của panel.

        NetworkMode của container không đổi được sau khi tạo; container còn trỏ tới network đã
        xóa thì Docker không khởi động lại được nữa (kể cả khi server reboot).
        """
        info = self.inspect(name)
        old_network = (info.get("HostConfig") or {}).get("NetworkMode")
        networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
        skip = {name, (info.get("Id") or "")[:12]}
        running = bool((info.get("State") or {}).get("Running"))
        backup = f"{name}-before-rename"
        if running:
            _query(["stop", name], timeout=120)
        _query(["rename", name, backup])
        try:
            self.create(name, create_body(info, self.network))
            for net, endpoint in networks.items():
                if net not in (self.network, old_network):
                    cmd = ["network", "connect"]
                    for alias in (endpoint or {}).get("Aliases") or []:
                        if alias not in skip:
                            cmd += ["--alias", alias]
                    _query([*cmd, net, name])
            if running:
                _query(["start", name], timeout=120)
        except DockerError:
            if self.container_exists(name):
                _query(["rm", "-f", name])
            _query(["rename", backup, name])
            raise
        # Không kèm -v: volume ẩn danh đã được container mới dùng lại
        _query(["rm", "-f", backup])

    def create(self, name: str, body: dict) -> None:
        _api("POST", f"/containers/create?name={urllib.parse.quote(name)}", body)

    def rename(self, name: str, new_name: str) -> None:
        _query(["rename", name, new_name])

    def pull_image(self, image: str, timeout: float = 900) -> None:
        """Kéo image qua Engine API: docker CLI mới tự hỏi đăng nhập khi image private, làm treo."""
        last = image.rsplit("/", 1)[-1]
        repo, tag = (image.rsplit(":", 1) if ":" in last else (image, "latest"))
        conn = _UnixConnection(os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock"), timeout)
        try:
            conn.request("POST", f"/images/create?{urllib.parse.urlencode({'fromImage': repo, 'tag': tag})}")
            res = conn.getresponse()
            # Luồng JSON từng dòng tiến độ; lỗi giữa chừng nằm ở dòng có "error"
            lines = [line for line in res.read().decode(errors="replace").splitlines() if line.strip()]
        except OSError as e:
            raise DockerError(t("docker.call_failed", command="pull", error=e)) from e
        finally:
            conn.close()
        errors = [json.loads(line).get("error") or json.loads(line).get("message") for line in lines
                  if '"error"' in line or res.status >= 400]
        if res.status >= 400 or any(errors):
            raise DockerError(next((e for e in errors if e), f"HTTP {res.status}"))

    def image_config(self, image: str) -> dict:
        data = json.loads(_query(["image", "inspect", image]))
        return (data[0] if data else {}).get("Config") or {}

    def migrate_network(self, old: str) -> list[str]:
        """Chuyển mọi container từ network `old` sang network của panel, giữ alias, rồi xóa `old`.

        Dùng khi đổi tên network (VibePanel -> OpsVibes): app và database vẫn gọi nhau bằng
        cùng tên mà không phải deploy lại. Trả về tên các container đã chuyển.
        """
        try:
            info = json.loads(_query(["network", "inspect", old]))[0]
        except DockerError:
            return []
        self.ensure_network()
        moved = []
        for container_id in info.get("Containers") or {}:
            try:
                data = self.inspect(container_id)
            except DockerError:
                continue
            name = data.get("Name", "").lstrip("/")
            networks = (data.get("NetworkSettings") or {}).get("Networks") or {}
            aliases = [a for a in (networks.get(old) or {}).get("Aliases") or [] if a != name and a != container_id[:12]]
            if self.network not in networks:
                cmd = ["network", "connect"]
                for alias in aliases:
                    cmd += ["--alias", alias]
                _query([*cmd, self.network, container_id])
            _query(["network", "disconnect", old, container_id])
            moved.append(name)
        try:
            _query(["network", "rm", old])
        except DockerError:
            pass
        return moved

    # ---------- Image ----------

    def nixpacks_plan(self, workspace: Path, env: dict[str, str]) -> dict:
        """Kế hoạch build của Nixpacks (để biết nó có tìm ra lệnh khởi động không)."""
        cmd = ["nixpacks", "plan", str(workspace), "--format", "json"]
        # Chỉ biến NIXPACKS_* ảnh hưởng tới kế hoạch; không đưa secret lên dòng lệnh thêm lần nữa
        for key, value in env.items():
            if key.startswith("NIXPACKS_"):
                cmd += ["--env", f"{key}={value}"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DockerError(t("docker.nixpacks_plan_error", error=e)) from e
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or t("docker.nixpacks_plan_failed"))
        return json.loads(result.stdout)

    def build_nixpacks(
        self, workspace: Path, image: str, env: dict[str, str], log: JobLogger, timeout: float,
        start_cmd: str | None = None, install_cmd: str | None = None, build_cmd: str | None = None,
    ) -> None:
        cmd = ["nixpacks", "build", str(workspace), "--name", image]
        for key, value in env.items():
            cmd += ["--env", f"{key}={value}"]
        for flag, value in (("--install-cmd", install_cmd), ("--build-cmd", build_cmd), ("--start-cmd", start_cmd)):
            if value:
                cmd += [flag, value]
        runner.run(cmd, log, timeout=timeout, env={"DOCKER_BUILDKIT": "1"})

    def build_dockerfile(self, workspace: Path, image: str, log: JobLogger, timeout: float,
                         build_args: dict[str, str] | None = None) -> None:
        # Biến của service thành build arg (Dockerfile khai báo ARG mới dùng, vd VITE_API_URL lúc build
        # frontend). "--build-arg TÊN" không kèm giá trị: docker lấy từ môi trường, secret không lộ trên dòng lệnh
        args = [x for key in (build_args or {}) for x in ("--build-arg", key)]
        runner.run(
            ["docker", "build", "--pull", *args, "-t", image, "-f", str(workspace / "Dockerfile"), str(workspace)],
            log,
            timeout=timeout,
            env={**(build_args or {}), "DOCKER_BUILDKIT": "1"},
        )

    def pull(self, image: str, log: JobLogger, timeout: float) -> None:
        runner.run(["docker", "pull", image], log, timeout=timeout)

    def image_exists(self, image: str) -> bool:
        try:
            _query(["image", "inspect", image])
            return True
        except DockerError:
            return False

    def exposed_ports(self, image: str) -> list[int]:
        raw = _query(["image", "inspect", "--format", "{{json .Config.ExposedPorts}}", image]).strip()
        ports = json.loads(raw) if raw and raw != "null" else {}
        return sorted(int(p.split("/")[0]) for p in ports if p.endswith("/tcp"))

    def remove_image(self, image: str) -> None:
        try:
            _query(["image", "rm", image])
        except DockerError:
            pass  # Đang được container khác dùng hoặc đã bị xóa

    # ---------- Container ----------

    def run_app(
        self, name: str, image: str, env: dict[str, str], labels: dict[str, str], log: JobLogger,
        volumes: dict[str, str] | None = None, aliases: list[str] | None = None,
    ) -> None:
        # Biến môi trường đi qua file tạm 0600, không nằm trên dòng lệnh
        fd, env_path = tempfile.mkstemp(prefix="vp-env-")
        try:
            with os.fdopen(fd, "w") as f:
                for key, value in env.items():
                    if "\n" in value:
                        raise DockerError(t("docker.env_newline", key=key))
                    f.write(f"{key}={value}\n")
            cmd = [
                "docker", "run", "-d",
                "--name", name,
                "--network", self.network,
                "--restart", "unless-stopped",
                "--env-file", env_path,
                "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            ]
            for volume, mount in (volumes or {}).items():
                cmd += ["-v", f"{volume}:{mount}"]
            # Tên host nội bộ ổn định: container đổi tên mỗi lần deploy, alias thì không
            for alias in aliases or []:
                cmd += ["--network-alias", alias]
            for key, value in {MANAGED_LABEL: "true", **labels}.items():
                cmd += ["--label", f"{key}={value}"]
            runner.run([*cmd, image], log)
        finally:
            os.unlink(env_path)

    # ---------- Database ----------

    def run_database(
        self, name: str, image: str, env: dict[str, str], volume: str, mount: str, alias: str,
        labels: dict[str, str], cmd: list[str] | None,
    ) -> None:
        fd, env_path = tempfile.mkstemp(prefix="vp-env-")
        try:
            with os.fdopen(fd, "w") as f:
                for key, value in env.items():
                    f.write(f"{key}={value}\n")
            args = [
                "run", "-d", "--name", name,
                "--network", self.network, "--network-alias", alias,
                "--restart", "unless-stopped",
                "--env-file", env_path,
                "-v", f"{volume}:{mount}",
                "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
            ]
            for key, value in {MANAGED_LABEL: "true", **labels}.items():
                args += ["--label", f"{key}={value}"]
            # Lần đầu có thể phải kéo image
            _query([*args, image, *(cmd or [])], timeout=600)
        finally:
            os.unlink(env_path)

    def exec_ok(self, name: str, cmd: list[str], timeout: float = 15) -> bool:
        try:
            result = subprocess.run(
                ["docker", "exec", name, *cmd], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def exec_to_file(self, name: str, cmd: list[str], dest: Path, compress: bool, timeout: float) -> None:
        """Chạy lệnh trong container, ghi stdout ra file (nén gzip nếu `compress`), vd xuất bản sao lưu."""
        proc = subprocess.Popen(
            ["docker", "exec", name, *cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        errors: list[bytes] = []
        # Đọc stderr song song để lệnh không bị treo khi bộ đệm stderr đầy
        reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            with (gzip.open(dest, "wb") if compress else dest.open("wb")) as out:
                while chunk := proc.stdout.read(1024 * 1024):
                    out.write(chunk)
            proc.wait()
            reader.join(10)
        finally:
            timer.cancel()
        if proc.returncode != 0:
            message = b"".join(errors).decode(errors="replace").strip()[-2000:]
            raise DockerError(message or t("docker.export_exit", code=proc.returncode))

    def exec_stdin(self, name: str, cmd: list[str], chunks, log: JobLogger, timeout: float) -> None:
        """Chạy lệnh trong container, đẩy dữ liệu (vd file backup) vào stdin, ghi output ra log."""
        proc = subprocess.Popen(
            ["docker", "exec", "-i", name, *cmd],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        output: list[str] = []

        def _read() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode(errors="replace")
                output.append(line)
                log.write(line)

        reader = threading.Thread(target=_read, daemon=True)
        reader.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            assert proc.stdin is not None
            try:
                for chunk in chunks:
                    proc.stdin.write(chunk)
            except BrokenPipeError:
                pass  # Lệnh đã thoát, mã lỗi được báo bên dưới
            finally:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            proc.wait()
            reader.join(10)
        finally:
            timer.cancel()
        if proc.returncode != 0:
            raise DockerError(t("docker.import_exit", code=proc.returncode))

    def container_state(self, name: str) -> str | None:
        """running | restarting | exited | ...; None nếu không có container."""
        try:
            return _query(["container", "inspect", "--format", "{{.State.Status}}", name]).strip()
        except DockerError:
            return None

    # ---------- Dọn dẹp ----------

    def prune(self, build_cache_until: str) -> list[str]:
        """Dọn rác an toàn: build cache cũ, image treo (không tag), container panel đã dừng.

        Không đụng tới container/volume/image đang dùng hay không do panel tạo.
        Trả về dòng tổng kết của từng lệnh để ghi log.
        """
        out = []
        for args in (
            ["builder", "prune", "-f", "--filter", f"until={build_cache_until}"],
            ["image", "prune", "-f"],
            ["container", "prune", "-f", "--filter", f"label={MANAGED_LABEL}=true"],
            ["container", "prune", "-f", "--filter", f"label={LEGACY_MANAGED_LABEL}=true"],
        ):
            try:
                lines = _query(args, timeout=600).strip().splitlines()
                out.append(f"docker {' '.join(args[:2])}: {lines[-1] if lines else 'ok'}")
            except DockerError as e:
                out.append(f"docker {' '.join(args[:2])}: {e}")
        return out

    def stopped_containers(self) -> list[str]:
        """Tên các container đã dừng / lỗi / chưa từng chạy (mọi container trên máy)."""
        out = _query(["ps", "-a", "--filter", "status=exited", "--filter", "status=created",
                      "--filter", "status=dead", "--format", "{{.Names}}"])
        return [line for line in out.splitlines() if line]

    def used_image_ids(self) -> set[str]:
        ids = [line for line in _query(["ps", "-a", "-q", "--no-trunc"]).splitlines() if line]
        if not ids:
            return set()
        return {line.strip() for line in _query(["inspect", "--format", "{{.Image}}", *ids]).splitlines() if line.strip()}

    def list_images(self) -> dict[str, list[str]]:
        """ID image -> các tên (repo:tag); image không tên có danh sách rỗng."""
        result: dict[str, list[str]] = {}
        for line in _query(["images", "--no-trunc", "--format", "{{.ID}}|{{.Repository}}|{{.Tag}}"]).splitlines():
            image_id, repo, tag = (line.split("|") + ["", ""])[:3]
            refs = result.setdefault(image_id, [])
            if repo != "<none>" and tag != "<none>":
                refs.append(f"{repo}:{tag}")
        return result

    def container_image(self, name: str) -> str | None:
        try:
            return _query(["container", "inspect", "--format", "{{.Config.Image}}", name]).strip() or None
        except DockerError:
            return None

    def unused_volumes(self) -> list[str]:
        """Volume không gắn với container nào (kể cả container đã dừng)."""
        return [v for v in _query(["volume", "ls", "-q", "--filter", "dangling=true"]).splitlines() if v]

    def remove_image_force(self, ref: str) -> bool:
        try:
            _query(["image", "rm", ref], timeout=120)
            return True
        except DockerError:
            return False

    def stop(self, name: str) -> None:
        try:
            _query(["stop", "-t", "20", name], timeout=60)
        except DockerError:
            pass

    def restart(self, name: str) -> None:
        _query(["restart", name], timeout=120)

    def remove_volume(self, name: str) -> None:
        try:
            _query(["volume", "rm", "-f", name], timeout=60)
        except DockerError:
            pass

    def wait_for_port(self, name: str, port: int, timeout: float) -> bool:
        """Chờ container nhận kết nối TCP; panel và app chung network nên gọi được theo tên.

        App crash thì Docker (--restart unless-stopped) đưa về "restarting": dừng chờ ngay
        thay vì đợi hết timeout.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.container_state(name) != "running":
                return False
            try:
                with socket.create_connection((name, port), timeout=2):
                    return True
            except OSError:
                time.sleep(1)
        return False

    def wait_http(self, name: str, port: int, path: str, timeout: float) -> tuple[bool, str]:
        """Chờ GET http://<container>:<port><path> trả 2xx/3xx. Trả (được không, lỗi cuối cùng)."""
        deadline = time.monotonic() + timeout
        last = ""
        while time.monotonic() < deadline:
            if self.container_state(name) != "running":
                return False, t("docker.container_stopped")
            conn = http.client.HTTPConnection(name, port, timeout=5)
            try:
                conn.request("GET", path, headers={"User-Agent": "OpsVibes-healthcheck"})
                status = conn.getresponse().status
                if 200 <= status < 400:
                    return True, ""
                last = f"HTTP {status}"
            except (OSError, http.client.HTTPException) as e:
                last = str(e) or type(e).__name__
            finally:
                conn.close()
            time.sleep(2)
        return False, last

    def wait_stable(self, name: str, seconds: float) -> bool:
        """Worker không mở cổng: coi là lên được nếu chạy liên tục `seconds` giây, không bị khởi động lại."""
        def restarts() -> str | None:
            try:
                return _query(["container", "inspect", "--format", "{{.RestartCount}}", name]).strip()
            except DockerError:
                return None

        start = restarts()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.container_state(name) != "running":
                return False
            time.sleep(1)
        return self.container_state(name) == "running" and restarts() == start

    def is_running(self, name: str) -> bool:
        try:
            return _query(["container", "inspect", "--format", "{{.State.Running}}", name]).strip() == "true"
        except DockerError:
            return False

    def log_lines(self, name: str, tail: int) -> list[tuple[str, str]]:
        """[(stdout|stderr, dòng có timestamp)] xếp theo thời gian (docker logs trả hai luồng riêng)."""
        try:
            result = subprocess.run(
                ["docker", "logs", "--tail", str(tail), "--timestamps", name],
                capture_output=True, text=True, errors="replace", timeout=30, stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DockerError(t("docker.call_failed", command="logs", error=e)) from e
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or t("docker.logs_failed"))
        lines = [("stdout", line) for line in result.stdout.splitlines()]
        lines += [("stderr", line) for line in result.stderr.splitlines()]
        # sorted giữ nguyên thứ tự các dòng cùng thời điểm trong một luồng
        return sorted(lines, key=lambda item: _log_time(item[1]))

    def logs(self, name: str, tail: int) -> str:
        return "".join(line + "\n" for _, line in self.log_lines(name, tail))

    def remove_container(self, name: str) -> None:
        try:
            _query(["rm", "-f", name], timeout=60)
        except DockerError:
            pass

    def containers_for_service(self, service_id: int) -> list[str]:
        names: list[str] = []
        for label in (SERVICE_LABEL, LEGACY_SERVICE_LABEL):
            out = _query(["ps", "-a", "--filter", f"label={label}={service_id}", "--format", "{{.Names}}"])
            names += [line for line in out.splitlines() if line and line not in names]
        return names

    # ---------- Docker Compose ----------

    def compose_config(self, project: str, file: Path, env: dict[str, str]) -> dict:
        """Cấu hình compose đã giải (biến ${...} lấy từ env của service, đường dẫn thành tuyệt đối)."""
        try:
            result = subprocess.run(
                ["docker", "compose", "-p", project, "-f", str(file), "config", "--format", "json"],
                cwd=file.parent, env={**os.environ, **env}, capture_output=True, text=True, timeout=60,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DockerError(t("docker.call_failed", command="compose", error=e)) from e
        if result.returncode != 0:
            raise DockerError(result.stderr.strip() or t("docker.command_failed", command="compose config"))
        return json.loads(result.stdout)

    def compose_up(self, project: str, file: Path, log: JobLogger, timeout: float, build: bool) -> None:
        # --force-recreate: file gắn vào container (bind mount) nằm trong mã nguồn vừa thay mới
        runner.run(
            ["docker", "compose", "-p", project, "-f", str(file), "up", "-d", "--remove-orphans", "--force-recreate",
             "--build" if build else "--no-build"],
            log, timeout=timeout, env={"DOCKER_BUILDKIT": "1"},
        )

    def compose_containers(self, project: str, service: str | None = None) -> list[str]:
        args = ["ps", "-a", "--filter", f"label=com.docker.compose.project={project}"]
        if service:
            args += ["--filter", f"label=com.docker.compose.service={service}"]
        return [line for line in _query([*args, "--format", "{{.Names}}"]).splitlines() if line]

    def compose_remove(self, project: str, volumes: bool) -> None:
        """Gỡ container, network (và volume nếu `volumes`) của một compose project, không cần file compose."""
        label = f"label=com.docker.compose.project={project}"
        try:
            for name in self.compose_containers(project):
                self.remove_container(name)
            for net in _query(["network", "ls", "-q", "--filter", label]).split():
                _query(["network", "rm", net])
            if volumes:
                for volume in _query(["volume", "ls", "-q", "--filter", label]).split():
                    self.remove_volume(volume)
        except DockerError:
            pass

    # ---------- Dung lượng ----------

    def container_sizes(self) -> list[dict]:
        """Container do panel tạo: tên, trạng thái, nhãn, dung lượng ghi riêng của container và kích thước image."""
        out = "".join(
            _query(["ps", "-a", "-s", "--filter", f"label={label}=true", "--format", "{{json .}}"], timeout=120)
            for label in (MANAGED_LABEL, LEGACY_MANAGED_LABEL)
        )
        rows = []
        for line in out.splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            # "12.3kB (virtual 1.2GB)": phần đầu là lớp ghi của container, virtual gồm cả image
            size = d.get("Size") or ""
            writable = parse_size(size.split("(")[0])
            virtual = parse_size(size.split("virtual", 1)[1]) if "virtual" in size else writable
            labels = dict(kv.split("=", 1) for kv in (d.get("Labels") or "").split(",") if "=" in kv)
            rows.append({"name": d.get("Names", ""), "image": d.get("Image", ""), "state": d.get("State", ""),
                         "writable": writable, "image_size": max(virtual - writable, 0), "labels": labels})
        return rows

    def volume_sizes(self) -> dict[str, int]:
        """Tên volume -> byte, đọc mục "Local Volumes" của `docker system df -v` (chậm: Docker phải đo từng volume)."""
        out = _query(["system", "df", "-v"], timeout=300)
        sizes: dict[str, int] = {}
        in_volumes = False
        for line in out.splitlines():
            if line.startswith("VOLUME NAME"):
                in_volumes = True
                continue
            if in_volumes:
                parts = line.split()
                if not parts:
                    break
                if len(parts) >= 3:
                    sizes[parts[0]] = parse_size(parts[-1])
        return sizes

    def image_sizes(self, reference: str) -> dict[str, int]:
        """Image khớp `reference` (vd vp/*) -> byte."""
        out = _query(["image", "ls", "--filter", f"reference={reference}", "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}"],
                     timeout=60)
        return {name: parse_size(size) for name, _, size in (line.partition("\t") for line in out.splitlines()) if name}

    # ---------- Sao lưu volume ----------

    # Image nhỏ có tar, chạy một lần để đọc / ghi volume (không phụ thuộc image của app)
    VOLUME_HELPER = "alpine:3.20"

    def volumes_with_label(self, label: str) -> list[str]:
        return [v for v in _query(["volume", "ls", "-q", "--filter", f"label={label}"]).split() if v]

    def volume_copy(self, src: str, dst: str, timeout: float = 300) -> None:
        """Chép toàn bộ nội dung volume `src` sang volume `dst` (tạo mới nếu chưa có)."""
        _query(["run", "--rm", "--label", f"{MANAGED_LABEL}=true", "-v", f"{src}:/from:ro", "-v", f"{dst}:/to",
                self.VOLUME_HELPER, "sh", "-c", "cp -a /from/. /to/"], timeout=timeout)

    def volume_exists(self, name: str) -> bool:
        try:
            _query(["volume", "inspect", name])
            return True
        except DockerError:
            return False

    def volume_export(self, volume: str, dest: Path, timeout: float) -> None:
        """Nén toàn bộ volume thành tar.gz (đọc chỉ-đọc, app vẫn chạy)."""
        with dest.open("wb") as out:
            try:
                result = subprocess.run(
                    ["docker", "run", "--rm", "--label", f"{MANAGED_LABEL}=true", "-v", f"{volume}:/data:ro",
                     self.VOLUME_HELPER, "tar", "czf", "-", "-C", "/data", "."],
                    stdout=out, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                raise DockerError(t("docker.call_failed", command="run", error=e)) from e
        if result.returncode != 0:
            raise DockerError(result.stderr.decode(errors="replace").strip()[-2000:] or t("docker.export_exit", code=result.returncode))

    def volume_import(self, volume: str, src, timeout: float) -> None:
        """Xóa sạch volume rồi giải nén tar.gz đọc từ `src` (file object) vào đó."""
        script = "find /data -mindepth 1 -delete && tar xzf - -C /data"
        proc = subprocess.Popen(
            ["docker", "run", "--rm", "-i", "--label", f"{MANAGED_LABEL}=true", "-v", f"{volume}:/data",
             self.VOLUME_HELPER, "sh", "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
        )
        errors: list[bytes] = []
        reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
        reader.start()
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
        try:
            try:
                while chunk := src.read(1024 * 1024):
                    proc.stdin.write(chunk)
            except BrokenPipeError:
                pass
            proc.stdin.close()
            proc.wait()
            reader.join(10)
        finally:
            timer.cancel()
        if proc.returncode != 0:
            message = b"".join(errors).decode(errors="replace").strip()[-2000:]
            raise DockerError(message or t("docker.import_exit", code=proc.returncode))

    # ---------- Duyệt file trong container ----------

    # Liệt kê một thư mục bằng stat (có cả trong image busybox/alpine); đường dẫn truyền qua $1, không ghép vào lệnh
    _LIST_DIR = (
        'cd -- "$1" || exit 3; for f in .* *; do case "$f" in .|..) continue;; esac; '
        '[ -e "$f" ] || [ -L "$f" ] || continue; stat -c "%F|%s|%Y|%n" -- "$f" || exit 4; done'
    )

    def list_dir(self, name: str, path: str) -> list[dict]:
        """Thư mục trong container: [{name, type: dir|file|link, size, mtime}]."""
        try:
            result = subprocess.run(["docker", "exec", name, "sh", "-c", self._LIST_DIR, "sh", path],
                                    capture_output=True, text=True, errors="replace", timeout=30,
                                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DockerError(t("docker.call_failed", command="exec", error=e)) from e
        if result.returncode == 3:
            raise FileNotFoundError(path)
        if result.returncode != 0:
            raise DockerError(t("browse.no_tools") if "not found" in result.stderr else result.stderr.strip())
        entries = []
        for line in result.stdout.splitlines():
            kind, size, mtime, entry = (line.split("|", 3) + ["", "", ""])[:4]
            if not entry:
                continue
            kind = "dir" if kind == "directory" else "link" if "symbolic" in kind else "file"
            entries.append({"name": entry, "type": kind, "size": int(size or 0) if kind == "file" else 0,
                            "mtime": int(mtime or 0)})
        return entries

    # Sửa file trong container bằng sh, chạy bằng root để không vướng quyền khi app chạy bằng user thường
    # (node, www-data…). Ghi đè bằng `cat >` giữ nguyên quyền / chủ của file cũ; file / thư mục mới được giao
    # cho chủ của thư mục cha gần nhất đã có, để app vẫn ghi được. Mã thoát 3 = không có, 5 = đã có.
    _FS_OWN = ('d="$1"; top=; while [ ! -e "$d" ] && [ ! -L "$d" ]; do top="$d"; d=$(dirname -- "$d"); done; '
               'own() { if [ -n "$top" ]; then chown -R "$(stat -c %u:%g -- "$d")" -- "$top" 2>/dev/null || true; fi; }; ')
    _FS_OPS = {
        "write": _FS_OWN + '[ -d "$1" ] && exit 6; cat > "$1" || exit 1; own',
        "create": _FS_OWN + '[ -z "$top" ] && exit 5; cat > "$1" || exit 1; own',
        "mkdir": _FS_OWN + '[ -z "$top" ] && exit 5; mkdir -p -- "$1" || exit 1; own',
        "rename": '{ [ -e "$1" ] || [ -L "$1" ]; } || exit 3; { [ -e "$2" ] || [ -L "$2" ]; } && exit 5; mv -- "$1" "$2"',
        "delete": '{ [ -e "$1" ] || [ -L "$1" ]; } || exit 3; rm -rf -- "$1"',
    }

    def fs_change(self, name: str, op: str, paths: list[str], data=None, timeout: float = 60) -> None:
        """Thao tác ghi trong container; `data`: bytes hoặc file mở sẵn, đẩy vào stdin (write / create)."""
        stdin = {"input": data} if isinstance(data, bytes) else {"stdin": data if data is not None else subprocess.DEVNULL}
        try:
            result = subprocess.run(["docker", "exec", "-i", "-u", "0", name, "sh", "-c", self._FS_OPS[op], "sh", *paths],
                                    capture_output=True, timeout=timeout, **stdin)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise DockerError(t("docker.call_failed", command="exec", error=e)) from e
        message = result.stderr.decode(errors="replace").strip()
        if result.returncode == 3:
            raise FileNotFoundError(paths[0])
        if result.returncode == 5:
            raise FileExistsError(paths[-1])
        if result.returncode == 6:
            raise IsADirectoryError(paths[0])
        if result.returncode in (126, 127):
            raise DockerError(t("browse.no_tools_edit"))
        if result.returncode != 0:
            raise DockerError(message or t("docker.command_failed", command="exec"))

    def copy_out(self, name: str, path: str, dest: Path, timeout: float) -> None:
        """`docker cp` một file / thư mục trong container ra file tar (không cần công cụ gì trong container)."""
        with dest.open("wb") as out:
            try:
                result = subprocess.run(["docker", "cp", f"{name}:{path}", "-"], stdout=out, stderr=subprocess.PIPE,
                                        timeout=timeout, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise DockerError(t("docker.call_failed", command="cp", error=e)) from e
        if result.returncode != 0:
            message = result.stderr.decode(errors="replace").strip()
            if "Could not find the file" in message or "No such" in message:
                raise FileNotFoundError(path)
            raise DockerError(message or t("docker.command_failed", command="cp"))

    # ---------- Traefik ----------

    def container_label(self, name: str, label: str) -> str | None:
        try:
            raw = _query(["container", "inspect", "--format", "{{json .Config.Labels}}", name]).strip()
        except DockerError:
            return None
        return (json.loads(raw) or {}).get(label)

    def start(self, name: str) -> None:
        _query(["start", name])

    def run_raw(self, args: list[str]) -> None:
        _query(["run", *args], timeout=300)

    # ---------- AI Agent ----------

    def list_all(self) -> str:
        """Mọi container trên máy (cả container không do panel tạo), dạng bảng."""
        return _query(["ps", "-a", "--format", "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"])

    def inspect(self, name: str) -> dict:
        data = json.loads(_query(["container", "inspect", name]))
        return data[0] if data else {}

    def exec_capture(self, name: str, command: str, timeout: float) -> tuple[int, str]:
        """Chạy lệnh sh trong container, trả (mã thoát, stdout+stderr); không ném lỗi khi lệnh lỗi."""
        return _capture(["docker", "exec", name, "sh", "-c", command], timeout)

    def host_capture(self, image: str, command: str, timeout: float, name: str | None = None) -> tuple[int, str]:
        """Chạy lệnh sh trên chính máy chủ (ngoài container panel) qua nsenter vào namespace của PID 1.

        Container dùng một lần, privileged: chỉ gọi sau khi người dùng đã duyệt lệnh. `name` đặt trước để
        dừng giữa chừng được: xóa container (docker rm -f) là kill mọi tiến trình của nó.
        """
        name = name or f"vp_host_{os.getpid()}_{threading.get_ident()}_{int(time.time() * 1000)}"
        try:
            return _capture([
                "docker", "run", "--rm", "--name", name, "--privileged", "--pid=host", "--network=host",
                "--label", f"{MANAGED_LABEL}=true", "--entrypoint", "nsenter", image,
                "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "/bin/sh", "-c", command,
            ], timeout)
        finally:
            self.remove_container(name)

    def host_start(self, name: str, image: str, command: str) -> None:
        """Chạy lệnh sh trên host trong một container nền giữ stdin (-dit), để sau còn gửi tiếp dữ liệu.

        Dùng cho đăng nhập agy: tiến trình phải sống giữa lúc lấy link và lúc dán mã.
        """
        self.remove_container(name)
        _query([
            "run", "-dit", "--name", name, "--privileged", "--pid=host", "--network=host",
            "--label", f"{MANAGED_LABEL}=true", "--entrypoint", "nsenter", image,
            "-t", "1", "-m", "-u", "-i", "-n", "-p", "--", "/bin/sh", "-lc", command,
        ], timeout=60)

    def attach_write(self, name: str, text: str, timeout: float = 30) -> None:
        """Gửi một dòng vào stdin của container nền (vd dán mã đăng nhập agy)."""
        subprocess.run(
            ["docker", "attach", "--sig-proxy=false", "--detach-keys=ctrl-c", name],
            input=text, text=True, capture_output=True, timeout=timeout,
        )

    def container_exists(self, name: str) -> bool:
        try:
            _query(["container", "inspect", "--format", "{{.Id}}", name])
            return True
        except DockerError:
            return False

    def resolve_ip(self, name: str) -> str | None:
        try:
            return socket.gethostbyname(name)
        except OSError:
            return None
