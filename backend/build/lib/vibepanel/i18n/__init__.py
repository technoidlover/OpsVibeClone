"""Song ngữ Việt / Anh cho thông báo của backend (lỗi API, log deploy, lỗi job).

- Request từ giao diện: ngôn ngữ lấy từ header Accept-Language (middleware đặt
  vào contextvar, nên cả validator của pydantic cũng dùng được).
- Job, webhook, việc chạy nền: không có request, dùng ngôn ngữ mặc định của
  panel (người dùng chọn gần nhất trên giao diện, lưu trong DB).

Bản dịch nằm trong các module con (`api.py`, `core.py`), mỗi module một cặp
dict VI / EN cùng khóa. Tham số dạng {name}, thay bằng str.format.
"""

from contextvars import ContextVar

from . import api, core

LANGS = ("vi", "en")
DEFAULT = "vi"

_CATALOGS: dict[str, dict[str, str]] = {lang: {} for lang in LANGS}
for _module in (api, core):
    _CATALOGS["vi"].update(_module.VI)
    _CATALOGS["en"].update(_module.EN)

_request_lang: ContextVar[str | None] = ContextVar("opsvibes_lang", default=None)
_panel_lang = DEFAULT


def t(key: str, /, **params) -> str:
    lang = current()
    message = _CATALOGS[lang].get(key) or _CATALOGS[DEFAULT].get(key) or key
    return message.format(**params) if params else message


def current() -> str:
    return _request_lang.get() or _panel_lang


def set_request_lang(lang: str | None):
    return _request_lang.set(lang)


def reset_request_lang(token) -> None:
    _request_lang.reset(token)


def set_panel_lang(lang: str | None) -> None:
    global _panel_lang
    _panel_lang = lang if lang in LANGS else DEFAULT


def parse_accept_language(header: str | None) -> str | None:
    """Ngôn ngữ đầu tiên được hỗ trợ trong header (vd "en-US,en;q=0.9" -> "en")."""
    for part in (header or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return None
