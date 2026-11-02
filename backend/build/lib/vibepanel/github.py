"""Kết nối GitHub kiểu Vercel bằng GitHub App riêng của mỗi panel.

Luồng:
1. Panel gửi một "manifest" sang GitHub; người dùng bấm xác nhận và GitHub
   tạo App dưới tài khoản (hoặc tổ chức) của họ, rồi trả về khóa của App.
2. Người dùng cài App lên tài khoản/tổ chức và chọn repo cho panel truy cập.
3. Panel liệt kê repo, clone và nhận webhook push bằng token cài đặt ngắn hạn
   (1 giờ) xin từ GitHub mỗi lần cần. Không lưu token lâu dài nào của người dùng.
"""

import base64
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy.orm import Session

from . import panel_settings
from .crypto import SecretBox
from .i18n import t
from .models import GitHubApp

API = "https://api.github.com"
WEB = "https://github.com"
REQUEST_TIMEOUT = 20
MANIFEST_STATE = "github_manifest_state"
STATE_TTL = 3600


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        # Mã HTTP GitHub trả về (None nếu lỗi mạng)
        self.status = status


def _request(method: str, url: str, token: str | None = None, body: dict | None = None) -> dict | list:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "OpsVibes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            raw = res.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read()).get("message", "")
        except (ValueError, AttributeError):
            message = ""
        text = (t("github.http_error_message", code=e.code, message=message) if message
                else t("github.http_error", code=e.code))
        raise GitHubError(text, e.code) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise GitHubError(t("github.unreachable", reason=getattr(e, "reason", e))) from e


# ---------- Manifest: tạo App ----------

def new_state(db: Session) -> str:
    """State chống giả mạo cho bước GitHub chuyển hướng về panel.

    Cookie phiên là SameSite=strict nên không đi kèm lượt chuyển hướng từ
    github.com; state ngẫu nhiên, dùng một lần, chỉ admin đã đăng nhập tạo được.
    """
    state = secrets.token_urlsafe(32)
    panel_settings.put(db, MANIFEST_STATE, f"{state}:{int(time.time()) + STATE_TTL}")
    db.commit()
    return state


def consume_state(db: Session, state: str) -> bool:
    stored = panel_settings.get(db, MANIFEST_STATE)
    panel_settings.put(db, MANIFEST_STATE, None)
    db.commit()
    if not stored or not state:
        return False
    expected, _, expires = stored.rpartition(":")
    return secrets.compare_digest(expected, state) and int(expires) > time.time()


def manifest(origin: str) -> dict:
    host = urllib.parse.urlsplit(origin).hostname or "panel"
    # Tên App phải duy nhất trên toàn GitHub, tối đa 34 ký tự
    name = f"OpsVibes {host[:19]} {secrets.token_hex(2)}"
    return {
        "name": name,
        "url": origin,
        "description": "Deploy repo lên OpsVibes tự host.",
        "public": False,
        "hook_attributes": {"url": f"{origin}/api/github/webhook", "active": True},
        "redirect_url": f"{origin}/api/github/manifest/callback",
        "setup_url": f"{origin}/api/github/setup",
        "setup_on_update": True,
        "default_permissions": {"contents": "read", "metadata": "read"},
        "default_events": ["push"],
    }


def manifest_action(organization: str | None, state: str) -> str:
    base = f"{WEB}/organizations/{urllib.parse.quote(organization)}" if organization else WEB
    return f"{base}/settings/apps/new?state={urllib.parse.quote(state)}"


def convert_manifest(db: Session, box: SecretBox, code: str) -> GitHubApp:
    """Đổi code GitHub trả về lấy khóa của App vừa tạo và lưu (mã hóa)."""
    data = _request("POST", f"{API}/app-manifests/{urllib.parse.quote(code)}/conversions")
    assert isinstance(data, dict)
    existing = db.get(GitHubApp, 1)
    if existing:
        db.delete(existing)
        db.flush()
    app = GitHubApp(
        id=1,
        app_id=data["id"],
        slug=data["slug"],
        name=data["name"],
        owner_login=(data.get("owner") or {}).get("login"),
        html_url=data["html_url"],
        client_id=data.get("client_id"),
        client_secret_enc=box.encrypt_optional(data.get("client_secret")),
        webhook_secret_enc=box.encrypt(data["webhook_secret"]),
        private_key_enc=box.encrypt(data["pem"]),
    )
    db.add(app)
    db.commit()
    _tokens.clear()
    return app


def get_app(db: Session) -> GitHubApp | None:
    return db.get(GitHubApp, 1)


def install_url(app: GitHubApp) -> str:
    return f"{WEB}/apps/{app.slug}/installations/new"


# ---------- Xác thực App ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: int, private_key_pem: str, now: float | None = None) -> str:
    """JWT RS256 để App xác thực với GitHub (hạn tối đa 10 phút)."""
    now = int(now if now is not None else time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    # Lùi iat 60 giây phòng lệch đồng hồ giữa server và GitHub
    payload = _b64(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    key = serialization.load_pem_private_key(private_key_pem.encode(), password=None)
    signature = key.sign(f"{header}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{header}.{payload}.{_b64(signature)}"


def _jwt(app: GitHubApp, box: SecretBox) -> str:
    return app_jwt(app.app_id, box.decrypt(app.private_key_enc))


# installation_id -> (token, hết hạn lúc); token sống 1 giờ, làm mới sớm 5 phút
_tokens: dict[int, tuple[str, float]] = {}
_tokens_lock = threading.Lock()


def installation_token(app: GitHubApp, box: SecretBox, installation_id: int) -> str:
    with _tokens_lock:
        cached = _tokens.get(installation_id)
        if cached and cached[1] - time.time() > 300:
            return cached[0]
    data = _request("POST", f"{API}/app/installations/{installation_id}/access_tokens", _jwt(app, box))
    assert isinstance(data, dict)
    token = data["token"]
    with _tokens_lock:
        _tokens[installation_id] = (token, time.time() + 3600)
    return token


# ---------- Đọc dữ liệu ----------

@dataclass
class Installation:
    id: int
    account: str
    account_type: str
    avatar_url: str | None
    repository_selection: str
    settings_url: str


@dataclass
class Repo:
    id: int
    installation_id: int
    full_name: str
    name: str
    owner: str
    private: bool
    default_branch: str
    clone_url: str
    html_url: str
    description: str | None
    pushed_at: str | None


def set_webhook_url(app: GitHubApp, box: SecretBox, origin: str) -> None:
    """Đổi địa chỉ webhook của App (vd sau khi đổi tên miền panel), không thì push không tới panel nữa."""
    _request("PATCH", f"{API}/app/hook/config", _jwt(app, box), {"url": f"{origin}/api/github/webhook"})


def installations(app: GitHubApp, box: SecretBox) -> list[Installation]:
    data = _request("GET", f"{API}/app/installations?per_page=100", _jwt(app, box))
    assert isinstance(data, list)
    return [
        Installation(
            id=i["id"],
            account=i["account"]["login"],
            account_type=i["account"].get("type", "User"),
            avatar_url=i["account"].get("avatar_url"),
            repository_selection=i.get("repository_selection", "selected"),
            settings_url=i.get("html_url") or f"{WEB}/settings/installations/{i['id']}",
        )
        for i in data
    ]


def _repo(installation_id: int, r: dict) -> Repo:
    return Repo(
        id=r["id"],
        installation_id=installation_id,
        full_name=r["full_name"],
        name=r["name"],
        owner=r["owner"]["login"],
        private=bool(r.get("private")),
        default_branch=r.get("default_branch") or "main",
        clone_url=r["clone_url"],
        html_url=r["html_url"],
        description=r.get("description"),
        pushed_at=r.get("pushed_at"),
    )


def repos(app: GitHubApp, box: SecretBox) -> list[Repo]:
    result: list[Repo] = []
    for inst in installations(app, box):
        token = installation_token(app, box, inst.id)
        for page in range(1, 11):
            data = _request("GET", f"{API}/installation/repositories?per_page=100&page={page}", token)
            assert isinstance(data, dict)
            batch = data.get("repositories", [])
            result += [_repo(inst.id, r) for r in batch]
            if len(batch) < 100:
                break
    result.sort(key=lambda r: r.pushed_at or "", reverse=True)
    return result


def repo(app: GitHubApp, box: SecretBox, installation_id: int, repo_id: int) -> Repo:
    """Một repo theo ID; lỗi nếu App không còn quyền truy cập repo đó."""
    data = _request("GET", f"{API}/repositories/{repo_id}", installation_token(app, box, installation_id))
    assert isinstance(data, dict)
    return _repo(installation_id, data)


def branches(app: GitHubApp, box: SecretBox, installation_id: int, full_name: str) -> list[str]:
    token = installation_token(app, box, installation_id)
    names: list[str] = []
    for page in range(1, 6):
        data = _request("GET", f"{API}/repos/{full_name}/branches?per_page=100&page={page}", token)
        assert isinstance(data, list)
        names += [b["name"] for b in data]
        if len(data) < 100:
            break
    return names


def read_file(app: GitHubApp, box: SecretBox, installation_id: int, full_name: str, path: str, ref: str) -> str | None:
    """Nội dung một file trong repo (None nếu không có); chỉ đọc file nhỏ."""
    return read_text(installation_token(app, box, installation_id), full_name, path, ref)


def tree(token: str, full_name: str, ref: str) -> list[str]:
    """Đường dẫn mọi file trong repo ở `ref` (một request; repo cực lớn có thể bị GitHub cắt bớt)."""
    url = f"{API}/repos/{full_name}/git/trees/{urllib.parse.quote(ref, safe='')}?recursive=1"
    data = _request("GET", url, token)
    entries = data.get("tree", []) if isinstance(data, dict) else []
    return [e["path"] for e in entries if e.get("type") == "blob" and isinstance(e.get("path"), str)]


def read_text(token: str, full_name: str, path: str, ref: str) -> str | None:
    url = f"{API}/repos/{full_name}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref)}"
    try:
        data = _request("GET", url, token)
    except GitHubError as e:
        if e.status == 404 or "HTTP 404" in str(e):
            return None
        raise
    if not isinstance(data, dict) or data.get("encoding") != "base64" or data.get("size", 0) > 512 * 1024:
        return None
    return base64.b64decode(data.get("content", "")).decode(errors="replace")


# GitHub chỉ gửi tối đa 20 commit trong payload push; nhiều hơn thì không biết hết file đã đổi
PUSH_COMMIT_LIMIT = 20


def push_touches(payload: dict, root_dir: str | None) -> bool:
    """Push có đổi file trong thư mục `root_dir` không (monorepo: chỉ deploy service bị ảnh hưởng).

    Không chắc chắn (không có danh sách commit, quá nhiều commit, force push) thì coi như có.
    """
    if not root_dir:
        return True
    commits = payload.get("commits")
    if not isinstance(commits, list) or not commits or len(commits) >= PUSH_COMMIT_LIMIT or payload.get("forced"):
        return True
    prefix = root_dir.strip("/") + "/"
    for commit in commits:
        for key in ("added", "modified", "removed"):
            for path in commit.get(key) or []:
                if isinstance(path, str) and path.startswith(prefix):
                    return True
    return False
