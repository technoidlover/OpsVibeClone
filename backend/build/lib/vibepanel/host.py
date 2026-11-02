"""Đo tài nguyên máy chủ (CPU, RAM, ổ đĩa, mạng, uptime) và giữ lịch sử ngắn.

Đọc từ /proc. Trong container, /proc/stat, meminfo, loadavg, uptime vẫn là số
liệu của host, nhưng /proc/net/dev chỉ thấy mạng của container. Vì vậy image
mount /proc của host vào /host/proc (chỉ đọc) và đọc mạng từ
/host/proc/1/net/dev (namespace mạng của tiến trình init trên host).
Ổ đĩa đo qua thư mục dữ liệu (bind mount từ host).
Trên máy không có /proc (vd macOS khi dev) các trường trả về None.
"""

import logging
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Card mạng ảo do Docker/hệ thống tạo: không tính vào lưu lượng của server
_VIRTUAL_IFACES = ("lo", "veth", "docker", "br-", "virbr", "cni", "flannel", "tun", "tap")


def _cpu_times(proc: Path) -> tuple[int, int, int]:
    """(busy, total, số nhân) từ dòng 'cpu' tổng của /proc/stat."""
    lines = (proc / "stat").read_text().splitlines()
    fields = [int(v) for v in lines[0].split()[1:]]
    idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
    total = sum(fields[:8])  # guest/guest_nice đã nằm trong user/nice
    cores = sum(1 for line in lines if line.startswith("cpu") and line[3:4].isdigit())
    return total - idle, total, cores


def _net_bytes(proc: Path) -> tuple[int, int]:
    """Tổng byte nhận/gửi qua các card mạng thật."""
    path = proc / "1" / "net" / "dev"
    if not path.exists():
        path = proc / "net" / "dev"
    rx = tx = 0
    for line in path.read_text().splitlines()[2:]:
        name, _, data = line.partition(":")
        if name.strip().startswith(_VIRTUAL_IFACES):
            continue
        cols = data.split()
        rx += int(cols[0])
        tx += int(cols[8])
    return rx, tx


def memory(proc: Path) -> dict | None:
    try:
        info = {}
        for line in (proc / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0]) * 1024
        total = info["MemTotal"]
        used = total - info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": used, "percent": round(used / total * 100, 1)}
    except (OSError, ValueError, KeyError, IndexError, ZeroDivisionError):
        return None


def disk(path: Path) -> dict | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    # Tính như `df`: bỏ phần dự trữ cho root khỏi mẫu số
    usable = usage.used + usage.free
    return {"total": usage.total, "used": usage.used, "percent": round(usage.used / usable * 100, 1) if usable else 0.0}


def uptime(proc: Path) -> int | None:
    try:
        return int(float((proc / "uptime").read_text().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def loadavg(proc: Path) -> list[float] | None:
    try:
        return [float(v) for v in (proc / "loadavg").read_text().split()[:3]]
    except (OSError, ValueError):
        return None


@dataclass
class _Previous:
    at: float
    cpu: tuple[int, int] | None
    net: tuple[int, int] | None


@dataclass
class Sampler:
    """Lấy mẫu định kỳ trong luồng nền; API chỉ đọc kết quả có sẵn."""

    proc: Path
    disk_path: Path
    interval: float = 5.0
    size: int = 60
    current: dict = field(default_factory=dict)
    history: dict[str, deque] = field(init=False)
    _prev: _Previous | None = field(default=None, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def __post_init__(self):
        self.history = {k: deque(maxlen=self.size) for k in ("cpu", "memory", "disk", "net_in", "net_out")}

    def sample(self) -> dict:
        now = time.monotonic()
        prev = self._prev
        cpu_raw = net_raw = None
        cores = 0
        try:
            busy, total, cores = _cpu_times(self.proc)
            cpu_raw = (busy, total)
        except (OSError, ValueError, IndexError):
            pass
        try:
            net_raw = _net_bytes(self.proc)
        except (OSError, ValueError, IndexError):
            pass

        cpu = None
        if cpu_raw and prev and prev.cpu and cpu_raw[1] > prev.cpu[1]:
            pct = (cpu_raw[0] - prev.cpu[0]) / (cpu_raw[1] - prev.cpu[1]) * 100
            cpu = {"percent": round(max(0.0, min(pct, 100.0)), 1), "cores": cores, "load": loadavg(self.proc) or []}
        network = None
        if net_raw and prev and prev.net and now > prev.at:
            dt = now - prev.at
            # Bộ đếm có thể reset (card mạng khởi động lại): không cho ra số âm
            network = {
                "rx_rate": max(0, round((net_raw[0] - prev.net[0]) / dt)),
                "tx_rate": max(0, round((net_raw[1] - prev.net[1]) / dt)),
            }
        self._prev = _Previous(now, cpu_raw, net_raw)

        snap = {
            "cpu": cpu,
            "memory": memory(self.proc),
            "disk": disk(self.disk_path),
            "network": network,
            "uptime_seconds": uptime(self.proc),
        }
        with self._lock:
            self.current = snap
            if cpu:
                self.history["cpu"].append(cpu["percent"])
            if snap["memory"]:
                self.history["memory"].append(snap["memory"]["percent"])
            if snap["disk"]:
                self.history["disk"].append(snap["disk"]["percent"])
            if network:
                self.history["net_in"].append(network["rx_rate"])
                self.history["net_out"].append(network["tx_rate"])
        return snap

    def read(self) -> dict:
        with self._lock:
            return {
                **self.current,
                "history": {k: list(v) for k, v in self.history.items()},
                "interval": self.interval,
            }

    def start(self) -> None:
        def loop():
            # Hai mẫu đầu cách nhau ngắn để sớm có % CPU và tốc độ mạng
            self.sample()
            if self._stop.wait(0.5):
                return
            while True:
                try:
                    self.sample()
                except Exception:  # noqa: BLE001 - luồng đo không được chết
                    logger.exception("Lỗi khi đo tài nguyên máy chủ")
                if self._stop.wait(self.interval):
                    return

        threading.Thread(target=loop, name="opsvibes-host-sampler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
