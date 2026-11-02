"""Google Drive làm nơi lưu sao lưu (cùng giao diện với s3.S3: key, put_file, get_file, list, delete, check).

Đăng nhập bằng OAuth device flow (như đăng nhập trên TV): panel hiện mã, người dùng mở
google.com/device để cho phép; không cần panel có tên miền / redirect URI. Quyền chỉ là
drive.file: panel chỉ thấy file do chính nó tạo. Người dùng tự tạo OAuth Client ID loại
"TVs and Limited Input devices" trong Google Cloud Console.

"Key" là đường dẫn dạng a/b/c/tên-file: mỗi phần là một thư mục trên Drive (tạo khi cần),
gốc là thư mục `root_name` trong My Drive.
"""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .s3 import S3Object

SCOPE = "https://www.googleapis.com/auth/drive.file"
DEVICE_URL = "https://oauth2.googleapis.com/device/code"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
# Drive yêu cầu mỗi phần tải lên (trừ phần cuối) là bội số của 256 KB
CHUNK = 64 * 1024 * 1024
TIMEOUT = 120


class DriveError(RuntimeError):
    pass


def _http(method: str, url: str, data: bytes | None = None, headers: dict | None = None,
          stream_to: Path | None = None) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            if stream_to is not None:
                with stream_to.open("wb") as out:
                    while chunk := res.read(1024 * 1024):
                        out.write(chunk)
                return res.status, b"", dict(res.headers)
            return res.status, res.read(), {k.lower(): v for k, v in res.headers.items()}
    except urllib.error.HTTPError as e:
        return e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}
    except OSError as e:
        raise DriveError(str(e) or type(e).__name__) from e


def _form(url: str, fields: dict) -> tuple[int, dict]:
    status, data, _ = _http("POST", url, urllib.parse.urlencode(fields).encode(),
                            {"Content-Type": "application/x-www-form-urlencoded"})
    try:
        return status, json.loads(data or b"{}")
    except ValueError:
        return status, {}


# ---------- Đăng nhập bằng mã thiết bị ----------

def device_start(client_id: str) -> dict:
    """{device_code, user_code, verification_url, expires_in, interval}."""
    status, body = _form(DEVICE_URL, {"client_id": client_id, "scope": SCOPE})
    if status != 200:
        raise DriveError(body.get("error_description") or body.get("error") or f"HTTP {status}")
    return body


def device_poll(client_id: str, client_secret: str, device_code: str) -> tuple[str, str | None]:
    """("pending" | "slow_down" | "ok" | "denied" | "expired" | "error", refresh_token hoặc lỗi)."""
    status, body = _form(TOKEN_URL, {
        "client_id": client_id, "client_secret": client_secret, "device_code": device_code,
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
    })
    if status == 200 and body.get("refresh_token"):
        return "ok", body["refresh_token"]
    error = body.get("error", "")
    if error == "authorization_pending":
        return "pending", None
    if error == "slow_down":
        return "slow_down", None
    if error == "access_denied":
        return "denied", None
    if error == "expired_token":
        return "expired", None
    return "error", body.get("error_description") or error or f"HTTP {status}"


# ---------- Client ----------

class Drive:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, root_name: str = "OpsVibes"):
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.root_name = root_name or "OpsVibes"
        self._token: str | None = None
        self._expires = 0.0
        self._folders: dict[str, str] = {}
        self._lock = threading.Lock()

    def key(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in parts if p)

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires - 60:
                return self._token
            status, body = _form(TOKEN_URL, {
                "client_id": self.client_id, "client_secret": self.client_secret,
                "refresh_token": self.refresh_token, "grant_type": "refresh_token",
            })
            if status != 200:
                raise DriveError(body.get("error_description") or body.get("error") or f"HTTP {status}")
            self._token = body["access_token"]
            self._expires = time.monotonic() + int(body.get("expires_in", 3600))
            return self._token

    def _api(self, method: str, url: str, data: bytes | None = None, headers: dict | None = None,
             stream_to: Path | None = None) -> tuple[bytes, dict]:
        status, body, res_headers = _http(method, url, data, {"Authorization": f"Bearer {self._access_token()}",
                                                              **(headers or {})}, stream_to)
        if status >= 300 and status != 308:
            try:
                message = json.loads(body)["error"]["message"]
            except (ValueError, KeyError, TypeError):
                message = f"HTTP {status}"
            raise DriveError(message)
        return body, res_headers

    def _json(self, method: str, url: str, payload: dict | None = None) -> dict:
        body, _ = self._api(method, url, json.dumps(payload).encode() if payload is not None else None,
                            {"Content-Type": "application/json"} if payload is not None else None)
        return json.loads(body or b"{}")

    def _children(self, parent: str, name: str | None = None, folders: bool | None = None) -> list[dict]:
        q = [f"'{parent}' in parents", "trashed = false"]
        if name is not None:
            q.append("name = '" + name.replace("\\", "\\\\").replace("'", "\\'") + "'")
        if folders is not None:
            q.append(f"mimeType {'=' if folders else '!='} '{FOLDER}'")
        out, token = [], None
        while True:
            params = {"q": " and ".join(q), "pageSize": "1000", "spaces": "drive",
                      "fields": "nextPageToken, files(id, name, size, modifiedTime, mimeType)"}
            if token:
                params["pageToken"] = token
            res = self._json("GET", f"{API}/files?{urllib.parse.urlencode(params)}")
            out += res.get("files", [])
            token = res.get("nextPageToken")
            if not token:
                return out

    def _folder(self, path: str, create: bool) -> str | None:
        """ID thư mục theo đường dẫn tính từ thư mục gốc của panel; None nếu chưa có và không tạo."""
        parent, walked = "root", ""
        for part in [self.root_name, *[p for p in path.split("/") if p]]:
            walked = f"{walked}/{part}"
            if walked in self._folders:
                parent = self._folders[walked]
                continue
            found = self._children(parent, part, folders=True)
            if found:
                parent = found[0]["id"]
            elif create:
                parent = self._json("POST", f"{API}/files?fields=id",
                                    {"name": part, "mimeType": FOLDER, "parents": [parent]})["id"]
            else:
                return None
            self._folders[walked] = parent
        return parent

    def _file(self, key: str) -> dict | None:
        folder, _, name = key.rpartition("/")
        parent = self._folder(folder, create=False)
        if parent is None:
            return None
        found = self._children(parent, name, folders=False)
        return found[0] if found else None

    def put_file(self, key: str, path: Path) -> None:
        folder, _, name = key.rpartition("/")
        parent = self._folder(folder, create=True)
        size = path.stat().st_size
        _, headers = self._api("POST", f"{UPLOAD}?uploadType=resumable",
                               json.dumps({"name": name, "parents": [parent]}).encode(),
                               {"Content-Type": "application/json; charset=UTF-8",
                                "X-Upload-Content-Type": "application/octet-stream",
                                "X-Upload-Content-Length": str(size)})
        session = headers.get("location")
        if not session:
            raise DriveError("No upload session")
        with path.open("rb") as f:
            offset = 0
            while True:
                chunk = f.read(CHUNK)
                end = offset + len(chunk) - 1
                content_range = f"bytes {offset}-{end}/{size}" if chunk else f"bytes */{size}"
                self._api("PUT", session, chunk, {"Content-Length": str(len(chunk)), "Content-Range": content_range})
                offset += len(chunk)
                if offset >= size:
                    return

    def get_file(self, key: str, path: Path) -> None:
        item = self._file(key)
        if item is None:
            raise DriveError(f"Not found: {key}")
        try:
            self._api("GET", f"{API}/files/{item['id']}?alt=media", stream_to=path)
        except DriveError:
            path.unlink(missing_ok=True)
            raise

    def delete(self, key: str) -> None:
        item = self._file(key)
        if item is not None:
            self._api("DELETE", f"{API}/files/{item['id']}")

    def list(self, prefix: str) -> list[S3Object]:
        parent = self._folder(prefix, create=False)
        if parent is None:
            return []
        out = []
        for item in self._children(parent, folders=False):
            modified = (item.get("modifiedTime") or "").replace("Z", "+00:00")
            out.append(S3Object(self.key(prefix, item["name"]), int(item.get("size") or 0),
                                datetime.fromisoformat(modified) if modified else datetime.now(timezone.utc)))
        return out

    def check(self) -> None:
        """Tạo thư mục gốc, ghi rồi xóa một file thử."""
        import tempfile

        with tempfile.NamedTemporaryFile() as f:
            f.write(b"ok")
            f.flush()
            self.put_file(self.key(".check", "opsvibes-check.txt"), Path(f.name))
        self.delete(self.key(".check", "opsvibes-check.txt"))
