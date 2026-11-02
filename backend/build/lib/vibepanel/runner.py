"""Chạy lệnh hệ thống an toàn cho các job build/deploy.

- Không bao giờ dùng shell: lệnh luôn là list[str].
- Mọi secret được thay bằng *** trước khi ghi ra log.
- Có timeout; quá hạn sẽ kill cả process group.
"""

import base64
import os
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

from .i18n import t


class CommandError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        super().__init__(message)
        # Output đã đọc được trước khi lệnh lỗi, để bên gọi còn phân tích
        self.output = output


class JobLogger:
    """Ghi log của một job ra file, tự che các secret đã đăng ký."""

    def __init__(self, path: Path):
        self.path = path
        self._secrets: set[str] = set()
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file: IO[str] = path.open("a", encoding="utf-8")

    def add_secret(self, value: str | None) -> None:
        # Bỏ qua chuỗi quá ngắn để không che nhầm nội dung bình thường
        if value and len(value) >= 4:
            self._secrets.add(value)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text

    def write(self, line: str) -> None:
        with self._lock:
            self._file.write(self.redact(line.rstrip("\n")) + "\n")
            self._file.flush()

    def close(self) -> None:
        self._file.close()


def run(
    cmd: list[str],
    log: JobLogger,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    log_cmd: str | None = None,
    on_line: Callable[[str], str | None] | None = None,
) -> str:
    """Chạy lệnh, stream stdout/stderr vào log và trả về toàn bộ output.

    `log_cmd` thay cho dòng lệnh khi ghi log (vd lệnh chứa prompt rất dài);
    `on_line` đổi từng dòng output thành dòng log dễ đọc, trả None để bỏ qua.
    """
    if not isinstance(cmd, list) or not cmd or not all(isinstance(part, str) for part in cmd):
        raise TypeError("cmd phải là list[str] không rỗng")

    log.write(f"$ {log_cmd or ' '.join(cmd)}")
    full_env = {**os.environ, **(env or {})}
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        start_new_session=True,
    )

    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(timeout, _kill) if timeout else None
    if timer:
        timer.start()

    output: list[str] = []
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            output.append(line)
            shown = on_line(line) if on_line else line
            if shown is not None:
                log.write(shown)
        proc.wait()
    finally:
        if timer:
            timer.cancel()

    text = "".join(output)
    if timed_out.is_set():
        raise CommandError(t("runner.timeout", timeout=timeout, cmd=cmd[0]), text)
    if proc.returncode != 0:
        raise CommandError(t("runner.exit_code", cmd=cmd[0], code=proc.returncode), text)
    return text


def git_auth_env(token: str | None) -> dict[str, str]:
    """Biến môi trường để git gửi token qua header HTTP.

    Token không nằm trong URL, trong tham số dòng lệnh (lộ qua `ps`) hay trong
    .git/config của workspace. Cần git >= 2.31.
    """
    env = {"GIT_TERMINAL_PROMPT": "0"}
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            }
        )
    return env
