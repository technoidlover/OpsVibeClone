"""Client S3 tối giản (AWS SigV4, thư viện chuẩn): đủ cho sao lưu ra ngoài VPS.

Dùng được với mọi dịch vụ S3-compatible: AWS S3, Cloudflare R2, Backblaze B2, Wasabi, MinIO...
Địa chỉ dạng path-style (https://<endpoint>/<bucket>/<key>), dịch vụ nào cũng hỗ trợ.
File lớn hơn PART_SIZE được tải lên theo multipart, mỗi phần đọc vào bộ nhớ một lần.
"""

import hashlib
import hmac
import http.client
import ssl
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

PART_SIZE = 64 * 1024 * 1024
TIMEOUT = 120
_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"


class S3Error(RuntimeError):
    pass


@dataclass
class S3Object:
    key: str
    size: int
    modified: datetime


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def sign(method: str, host: str, path: str, query: str, headers: dict[str, str], payload_hash: str,
         now: datetime, region: str, access_key: str, secret_key: str) -> dict[str, str]:
    """Header đã ký SigV4 (gồm host, x-amz-date, x-amz-content-sha256, authorization)."""
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    all_headers = {"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date,
                   **{k.lower(): v for k, v in headers.items()}}
    signed = ";".join(sorted(all_headers))
    canonical = "\n".join([
        method, path, query,
        "".join(f"{k}:{str(all_headers[k]).strip()}\n" for k in sorted(all_headers)),
        signed, payload_hash,
    ])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, _sha256(canonical.encode())])
    k = _sign(_sign(_sign(_sign(("AWS4" + secret_key).encode(), day), region), "s3"), "aws4_request")
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    all_headers["authorization"] = (f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
                                    f"SignedHeaders={signed}, Signature={signature}")
    return all_headers


class S3:
    def __init__(self, endpoint: str, region: str, bucket: str, access_key: str, secret_key: str, prefix: str = ""):
        url = urlparse(endpoint if "://" in endpoint else f"https://{endpoint}")
        self.scheme, self.host = url.scheme or "https", url.netloc
        self.region = region or "auto"
        self.bucket, self.access_key, self.secret_key = bucket, access_key, secret_key
        self.prefix = prefix.strip("/")

    def key(self, *parts: str) -> str:
        return "/".join(p.strip("/") for p in (self.prefix, *parts) if p)

    # ---------- HTTP + chữ ký ----------

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"",
                 headers: dict[str, str] | None = None, stream_to: Path | None = None) -> tuple[int, bytes, dict]:
        path = "/" + quote(self.bucket, safe="") + ("/" + quote(key, safe="/~") if key else "")
        canonical_query = "&".join(f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted((query or {}).items()))
        all_headers = sign(method, self.host, path, canonical_query, headers or {}, _sha256(body),
                           datetime.now(timezone.utc), self.region, self.access_key, self.secret_key)
        conn_cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
        kwargs = {"context": ssl.create_default_context()} if self.scheme == "https" else {}
        conn = conn_cls(self.host, timeout=TIMEOUT, **kwargs)
        try:
            # PUT/POST luôn gửi thân (kể cả rỗng) để có Content-Length, S3 bắt buộc
            conn.request(method, path + (f"?{canonical_query}" if canonical_query else ""),
                         body=body if method in ("PUT", "POST") else None, headers=all_headers)
            res = conn.getresponse()
            if stream_to is not None and res.status == 200:
                with stream_to.open("wb") as out:
                    while chunk := res.read(1024 * 1024):
                        out.write(chunk)
                return res.status, b"", dict(res.getheaders())
            return res.status, res.read(), {k.lower(): v for k, v in res.getheaders()}
        except (OSError, http.client.HTTPException) as e:
            raise S3Error(str(e) or type(e).__name__) from e
        finally:
            conn.close()

    @staticmethod
    def _error(status: int, data: bytes) -> S3Error:
        try:
            root = ET.fromstring(data)
            code, message = root.findtext("Code") or "", root.findtext("Message") or ""
            return S3Error(f"HTTP {status} {code}: {message}".strip(": "))
        except ET.ParseError:
            return S3Error(f"HTTP {status}")

    def _ok(self, method: str, key: str = "", **kw) -> tuple[bytes, dict]:
        status, data, headers = self._request(method, key, **kw)
        if status >= 300:
            raise self._error(status, data)
        return data, headers

    # ---------- Thao tác ----------

    def put_file(self, key: str, path: Path) -> None:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size <= PART_SIZE:
                self._ok("PUT", key, body=f.read())
                return
            data, _ = self._ok("POST", key, query={"uploads": ""})
            upload_id = ET.fromstring(data).findtext(f"{_NS}UploadId") or ET.fromstring(data).findtext("UploadId")
            parts = []
            try:
                number = 1
                while chunk := f.read(PART_SIZE):
                    _, headers = self._ok("PUT", key, query={"partNumber": str(number), "uploadId": upload_id}, body=chunk)
                    parts.append((number, headers.get("etag", "")))
                    number += 1
                body = ("<CompleteMultipartUpload>" + "".join(
                    f"<Part><PartNumber>{n}</PartNumber><ETag>{etag}</ETag></Part>" for n, etag in parts
                ) + "</CompleteMultipartUpload>").encode()
                self._ok("POST", key, query={"uploadId": upload_id}, body=body)
            except Exception:
                try:
                    self._request("DELETE", key, query={"uploadId": upload_id})
                except S3Error:
                    pass
                raise

    def get_file(self, key: str, path: Path) -> None:
        status, data, _ = self._request("GET", key, stream_to=path)
        if status != 200:
            path.unlink(missing_ok=True)
            raise self._error(status, data)

    def delete(self, key: str) -> None:
        self._ok("DELETE", key)

    def list(self, prefix: str) -> list[S3Object]:
        out: list[S3Object] = []
        token = None
        while True:
            p = prefix.strip("/")
            query = {"list-type": "2", "prefix": f"{p}/" if p else ""}
            if token:
                query["continuation-token"] = token
            data, _ = self._ok("GET", query=query)
            root = ET.fromstring(data)
            ns = _NS if root.tag.startswith(_NS) else ""
            for item in root.iter(f"{ns}Contents"):
                modified = (item.findtext(f"{ns}LastModified") or "").replace("Z", "+00:00")
                out.append(S3Object(item.findtext(f"{ns}Key") or "", int(item.findtext(f"{ns}Size") or 0),
                                    datetime.fromisoformat(modified) if modified else datetime.now(timezone.utc)))
            if (root.findtext(f"{ns}IsTruncated") or "").lower() != "true":
                return out
            token = root.findtext(f"{ns}NextContinuationToken")

    def check(self) -> None:
        """Ghi, liệt kê rồi xóa một file thử: đủ quyền cho sao lưu."""
        key = self.key(".opsvibes-check")
        self._ok("PUT", key, body=b"ok")
        self.list(self.prefix)
        self.delete(key)
