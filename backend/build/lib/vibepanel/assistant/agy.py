"""Antigravity CLI (agy) cài thẳng trên máy chủ, quản lý qua giao diện panel.

agy cài vào thư mục dữ liệu của panel (mặc định /etc/opsvibes/agy, là bind mount
từ host nên panel và host thấy cùng đường dẫn), HOME trỏ vào đó luôn. Mọi lệnh chạy
agy đều thực thi trên host qua nsenter (docker.host_capture / host_start), vì binary
agy là của host.

Đăng nhập Google không có cách không-tương-tác: agy in link + mã, người dùng mở link,
dán mã lại. Panel giữ một container đăng nhập nền để nối hai bước đó qua giao diện.

ToS: Google có thể khóa tài khoản khi phần mềm bên thứ ba điều khiển agy bằng tài
khoản cá nhân. Tính năng này tắt mặc định, có cảnh báo, người dùng tự quyết (dùng
trên VPS cá nhân của chính họ). Xem [[api-keys-only]] để biết vì sao API key là mặc định.
"""

import base64
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from ..docker import Docker, DockerError

# Đường dẫn trên host (trùng đường dẫn trong container nhờ bind mount DATA_DIR -> /etc/opsvibes)
AGY_HOME = "/etc/opsvibes/agy"
AGY_BIN = f"{AGY_HOME}/.local/bin/agy"
TOKEN_REL = ".gemini/antigravity-cli/antigravity-oauth-token"
LOG_GLOB = ".gemini/antigravity-cli/log/*.log"
LOGIN_CONTAINER = "opsvibes-agy-login"
INSTALL_URL = "https://antigravity.google/cli/install.sh"
# Wrapper để chỉ cần gõ `agy` từ bất kỳ shell nào mà vẫn dùng đúng HOME (phiên đăng nhập của panel)
WRAPPER = "/usr/local/bin/agy"
# Lệnh người dùng tự chạy qua SSH để đăng nhập (wrapper tự đặt HOME nên chỉ cần `sudo agy`)
SSH_LOGIN_HINT = f"sudo {WRAPPER}"

_GOOGLE_URL = re.compile(r"https://accounts\.google\.com/\S+")
# agy in mã dùng một lần kiểu "code: ABC-123" hoặc "enter the code" (chuẩn hóa nếu có)
_CODE = re.compile(r"code[:\s]+([A-Za-z0-9._-]{4,})", re.I)


class AgyError(RuntimeError):
    pass


def panel_image(docker: Docker, panel_container: str) -> str:
    """Image của chính panel: có sẵn, có nsenter, không phải kéo thêm."""
    try:
        image = (docker.inspect(panel_container).get("Config") or {}).get("Image")
    except DockerError:
        image = None
    if not image:
        raise AgyError("Không tìm được image panel để chạy lệnh trên máy chủ")
    return image


def _host(docker: Docker, image: str, command: str, timeout: float, name: str | None = None) -> tuple[int, str]:
    return docker.host_capture(image, command, timeout, name) if name else docker.host_capture(image, command, timeout)


@dataclass
class Status:
    installed: bool
    connected: bool
    version: str | None = None
    email: str | None = None
    logging_in: bool = False


def status(docker: Docker, image: str, data_dir: Path, with_version: bool = True) -> Status:
    """Trạng thái agy trên host. Đọc file trực tiếp (panel thấy cùng thư mục qua bind mount).

    with_version=False: bỏ qua lần gọi agy --version (chạy trên host, tốn một container),
    dùng khi poll liên tục lúc chờ người dùng đăng nhập.
    """
    home = data_dir / "agy"
    installed = (home / ".local" / "bin" / "agy").exists()
    token = home / TOKEN_REL
    connected = token.is_file() and token.stat().st_size > 0
    version = None
    if installed and with_version:
        code, out = _host(docker, image, f"HOME={AGY_HOME} {AGY_BIN} --version 2>/dev/null | head -n 1", 30)
        version = out.strip() or None if code == 0 else None
    email = None
    if connected:
        for log in sorted((home / ".gemini" / "antigravity-cli" / "log").glob("*.log"), reverse=True):
            m = re.search(r"applyAuthResult: email=([^,\s]+)", log.read_text(errors="replace"))
            if m:
                email = m.group(1)
                break
    return Status(installed=installed, connected=connected, version=version, email=email,
                  logging_in=docker.container_exists(LOGIN_CONTAINER))


def install(docker: Docker, image: str, timeout: float) -> str:
    """Cài / cập nhật agy trên host bằng installer chính thức (tự kiểm tra checksum)."""
    wrapper = f'printf \'#!/bin/sh\\nexec env HOME={AGY_HOME} {AGY_BIN} "$@"\\n\' > {WRAPPER}; chmod +x {WRAPPER}'
    cmd = (f"set -e; mkdir -p {AGY_HOME}; export HOME={AGY_HOME}; "
           f"curl -fsSL {INSTALL_URL} | bash; {wrapper}; {AGY_BIN} --version")
    code, out = _host(docker, image, cmd, timeout)
    if code != 0:
        raise AgyError(out.strip()[-800:] or f"Cài đặt thất bại (mã {code})")
    return out.strip()


def uninstall(docker: Docker, image: str) -> None:
    docker.remove_container(LOGIN_CONTAINER)
    _host(docker, image, f"rm -rf {AGY_HOME} {WRAPPER}", 60)


def logout(docker: Docker, image: str) -> None:
    docker.remove_container(LOGIN_CONTAINER)
    _host(docker, image, f"rm -f {AGY_HOME}/{TOKEN_REL}", 30)


@dataclass
class LoginPrompt:
    url: str | None
    code: str | None
    # Toàn bộ output agy đã in, để hiện khi không tự nhận ra link
    output: str


def _login_output(docker: Docker) -> str:
    try:
        return docker.logs(LOGIN_CONTAINER, 200)
    except DockerError:
        return ""


def login_start(docker: Docker, image: str, data_dir: Path) -> LoginPrompt:
    """Khởi động tiến trình đăng nhập agy trên host, chờ nó in link Google rồi trả về."""
    if status(docker, image, data_dir).connected:
        raise AgyError("Đã đăng nhập rồi")
    docker.remove_container(LOGIN_CONTAINER)
    # agy khởi động, nhận ra không có trình duyệt -> in link + chờ mã trên stdin (container giữ -it)
    docker.host_start(LOGIN_CONTAINER, image, f"HOME={AGY_HOME} exec {AGY_BIN}")
    import time
    url = None
    out = ""
    for _ in range(40):  # tối đa ~20 giây để agy in link
        time.sleep(0.5)
        out = _login_output(docker)
        m = _GOOGLE_URL.search(out)
        if m:
            url = m.group(0)
            break
        if not docker.container_exists(LOGIN_CONTAINER):
            break
    code = (_CODE.search(out).group(1) if _CODE.search(out) else None)
    return LoginPrompt(url=url, code=code, output=out[-2000:])


def login_submit(docker: Docker, image: str, data_dir: Path, code: str) -> Status:
    """Dán mã xác nhận vào tiến trình đăng nhập, chờ agy lưu phiên rồi dọn container."""
    if not docker.container_exists(LOGIN_CONTAINER):
        raise AgyError("Phiên đăng nhập đã hết hạn, bấm đăng nhập lại")
    docker.attach_write(LOGIN_CONTAINER, code.strip() + "\n")
    import time
    for _ in range(30):  # chờ agy hoàn tất và ghi token
        time.sleep(0.5)
        st = status(docker, image, data_dir)
        if st.connected:
            docker.remove_container(LOGIN_CONTAINER)
            return st
        if not docker.container_exists(LOGIN_CONTAINER):
            break
    out = _login_output(docker)
    docker.remove_container(LOGIN_CONTAINER)
    raise AgyError(out.strip()[-500:] or "Đăng nhập không thành công (mã sai hoặc đã hết hạn)")


def login_cancel(docker: Docker) -> None:
    docker.remove_container(LOGIN_CONTAINER)


# ---------- Chạy agy làm "bộ não" của agent (qua MCP của panel) ----------
# agy chạy ở network namespace của host nên gọi panel qua cổng publish 127.0.0.1:3000.
MCP_URL = "http://127.0.0.1:3000/api/assistant/mcp"
MCP_NAME = "opsvibes"
# Tên MCP server trước khi đổi tên sản phẩm, gỡ khỏi cấu hình agy cũ
LEGACY_MCP_NAME = "vibepanel"
# agy chỉ được dùng tool MCP của panel; tool shell/đọc/ghi file sẵn có của agy bị chặn
PERMISSIONS = {
    "allow": [f"mcp({MCP_NAME}/*)", "read_url(*)"],
    "deny": ["command(*)", "unsandboxed(*)", "write_file(*)", "read_file(*)", "execute_url(*)"],
}
_AGY_FAILED = {"ERROR", "CANCELED", "INTERRUPTED", "INVALID"}


def configure_mcp(docker: Docker, image: str, token: str, url: str = MCP_URL) -> None:
    """Ghi quyền (chỉ cho tool MCP) và trỏ agy tới MCP server của panel với token của lượt này."""
    settings = base64.b64encode(json.dumps({"permissions": PERMISSIONS}).encode()).decode()
    header = shlex.quote("Authorization: Bearer " + token)
    cmd = (f"set -e; mkdir -p {AGY_HOME}/.gemini/antigravity-cli; "
           f"printf %s {settings} | base64 -d > {AGY_HOME}/.gemini/antigravity-cli/settings.json; "
           f"export HOME={AGY_HOME}; "
           f"{AGY_BIN} mcp remove {MCP_NAME} >/dev/null 2>&1 || true; "
           f"{AGY_BIN} mcp remove {LEGACY_MCP_NAME} >/dev/null 2>&1 || true; "
           f"{AGY_BIN} mcp add --header {header} {MCP_NAME} {shlex.quote(url)}")
    code, out = _host(docker, image, cmd, 60)
    if code != 0:
        raise AgyError(out.strip()[-500:] or "Không cấu hình được MCP cho agy")


def run(docker: Docker, image: str, prompt: str, model: str | None, timeout: int,
        name: str | None = None) -> tuple[str | None, bool]:
    """Chạy agy headless một lượt; agy tự gọi tool MCP của panel. Trả (câu trả lời, có_lỗi).

    name: tên container chạy agy, để bấm Dừng thì xóa container đó (kill agy).
    """
    args = (f"cd {AGY_HOME}/work 2>/dev/null; HOME={AGY_HOME} {AGY_BIN} -p {shlex.quote(prompt)} "
            f"--output-format json --print-timeout {max(1, timeout // 60)}m")
    if model:
        args += f" --model {shlex.quote(model)}"
    code, out = _host(docker, image, f"mkdir -p {AGY_HOME}/work; {args}", timeout + 30, name)
    text, failed = parse_result(out)
    if code != 0 and not text:
        return out.strip()[-500:] or f"agy lỗi (mã {code})", True
    return text, failed


def parse_result(out: str) -> tuple[str | None, bool]:
    """Lấy object JSON kết quả cuối của agy (--output-format json)."""
    lines = [line for line in out.splitlines() if line.strip()]
    for i in range(len(lines) - 1, -1, -1):
        if not lines[i].lstrip().startswith("{"):
            continue
        for end in range(i + 1, len(lines) + 1):
            try:
                data = json.loads("\n".join(lines[i:end]))
            except ValueError:
                continue
            if isinstance(data, dict) and "status" in data:
                failed = data.get("status") in _AGY_FAILED or bool(data.get("error"))
                return data.get("response") or data.get("error"), failed
            break
    return None, True
