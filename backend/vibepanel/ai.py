"""Danh mục AI provider và gọi API của họ (chỉ dùng thư viện chuẩn).

Panel không tự chọn model thay người dùng: danh sách model lấy trực tiếp từ
API của provider bằng key người dùng nhập, nên không bị lỗi thời.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .i18n import t

REQUEST_TIMEOUT = 15


@dataclass(frozen=True)
class ProviderSpec:
    kind: str
    label: str
    base_url: str
    # "openai" = API tương thích OpenAI (GET /models với Bearer token)
    api: str
    key_url: str | None = None
    # Provider tự host (Ollama, LM Studio...) có thể không cần key
    key_required: bool = True
    # Nhãn không phải tên thương hiệu thì dịch theo ngôn ngữ (khóa trong i18n)
    label_key: str | None = None

    @property
    def display_label(self) -> str:
        return t(self.label_key) if self.label_key else self.label


PROVIDERS: dict[str, ProviderSpec] = {
    spec.kind: spec
    for spec in (
        ProviderSpec("anthropic", "Anthropic", "https://api.anthropic.com/v1", "anthropic",
                     "https://platform.claude.com/settings/keys"),
        ProviderSpec("openai", "OpenAI", "https://api.openai.com/v1", "openai",
                     "https://platform.openai.com/api-keys"),
        ProviderSpec("gemini", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta", "gemini",
                     "https://aistudio.google.com/apikey"),
        ProviderSpec("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "openrouter",
                     "https://openrouter.ai/settings/keys"),
        ProviderSpec("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "openai",
                     "https://platform.deepseek.com/api_keys"),
        ProviderSpec("xai", "xAI (Grok)", "https://api.x.ai/v1", "openai", "https://console.x.ai"),
        ProviderSpec("groq", "Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys"),
        ProviderSpec("mistral", "Mistral", "https://api.mistral.ai/v1", "openai",
                     "https://console.mistral.ai/api-keys"),
        ProviderSpec("custom", "Custom", "", "openai", key_required=False, label_key="ai.provider_custom"),
    )
}


class ProviderError(RuntimeError):
    pass


# Model không chat/gọi tool được (embedding, giọng nói, ảnh, video...): agent không dùng được nên không hiện
_NON_CHAT = (
    "embed", "tts", "whisper", "transcribe", "dall-e", "imagen", "image", "veo", "lyria", "moderation",
    "aqa", "audio", "realtime", "live", "gemma", "babbage", "davinci", "computer-use", "robotics",
)


def is_chat_model(model: str) -> bool:
    name = model.lower().rsplit("/", 1)[-1]
    return not any(part in name for part in _NON_CHAT)


def _get_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "OpsVibes", **headers})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise ProviderError(t("ai.key_rejected", code=e.code)) from e
        raise ProviderError(t("ai.http_error", code=e.code)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ProviderError(t("ai.connect_failed", host=urllib.parse.urlsplit(url).netloc, error=reason)) from e
    except ValueError as e:
        raise ProviderError(t("ai.not_json")) from e


def list_models(kind: str, base_url: str, api_key: str | None) -> list[str]:
    """Gọi API liệt kê model: vừa kiểm tra key, vừa lấy danh sách để chọn.

    Chỉ trả model chat và gọi tool được (agent cần cả hai).
    """
    spec = PROVIDERS[kind]
    base = base_url.rstrip("/")
    key = api_key or ""

    if spec.api == "anthropic":
        data = _get_json(f"{base}/models?limit=1000", {"x-api-key": key, "anthropic-version": "2023-06-01"})
        models = [m["id"] for m in data.get("data", [])]
    elif spec.api == "gemini":
        models, token = [], ""
        for _ in range(10):
            query = urllib.parse.urlencode({"pageSize": 1000, **({"pageToken": token} if token else {})})
            data = _get_json(f"{base}/models?{query}", {"x-goog-api-key": key})
            models += [
                m["name"].removeprefix("models/")
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
            ]
            token = data.get("nextPageToken", "")
            if not token:
                break
    else:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if spec.api == "openrouter":
            # /models của OpenRouter không cần key: gọi /key để chắc key hợp lệ
            _get_json(f"{base}/key", headers)
        data = _get_json(f"{base}/models", headers)
        items = [m for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        if spec.api == "openrouter":
            # OpenRouter ghi rõ model nào nhận tham số tools
            items = [m for m in items if "tools" in (m.get("supported_parameters") or ["tools"])]
        models = [m["id"] for m in items]

    return sorted({m for m in models if is_chat_model(m)})
