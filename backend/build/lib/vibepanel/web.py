"""Phục vụ giao diện web (bản build của frontend/) từ cùng process với API."""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

# Giao diện chỉ tải tài nguyên từ chính panel; Swagger UI (/docs) không áp dụng.
# Ngoại lệ cho kết nối GitHub: form gửi manifest tạo App sang github.com, và
# ảnh avatar của tài khoản đã cài App.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: https://avatars.githubusercontent.com; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self' https://github.com"
)


def mount_web(app: FastAPI, web_dir: Path) -> None:
    root = web_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not Found")
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root):
            # File trong assets/ có hash trong tên: cache vĩnh viễn
            cache = "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"
            return FileResponse(candidate, headers={"Cache-Control": cache})
        # Mọi đường dẫn còn lại để React Router xử lý
        return FileResponse(index, headers={"Cache-Control": "no-cache", "Content-Security-Policy": CSP})
