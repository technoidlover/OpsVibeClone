"""Gọi model có tool calling qua API của provider (chỉ dùng thư viện chuẩn).

Hội thoại lưu ở một dạng chung (giống Messages API của Anthropic), mỗi adapter
đổi sang định dạng của provider khi gọi:
- {"role": "user" | "assistant", "content": [block, ...]}
- block: {"type": "text", "text"}
        {"type": "tool_use", "id", "name", "input", "meta"?}
        {"type": "tool_result", "tool_use_id", "content", "is_error"}
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field

from .. import ai
from ..i18n import t


class RateLimitError(ai.ProviderError):
    """Provider trả 429 (hết quota / vượt tần suất). retry_after: số giây chờ, có thể None."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# "Please retry in 13h36m13.39s" (Gemini) → tổng số giây
_RETRY_RE = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", re.I)


def _retry_after(text: str) -> int | None:
    m = _RETRY_RE.search(text or "")
    if not m or not any(m.groups()):
        return None
    h, mn, s = m.groups()
    return int(float(h or 0) * 3600 + float(mn or 0) * 60 + float(s or 0))


def _fmt_delay(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    parts = ([f"{h}h"] if h else []) + ([f"{rem // 60}m"] if rem // 60 else [])
    return " ".join(parts) or "<1m"


def _rate_message(detail: str) -> str:
    delay = _retry_after(detail)
    msg = t("assistant.rate_limited")
    if delay:
        msg += " · " + t("assistant.retry_after", when=_fmt_delay(delay))
    return msg

REQUEST_TIMEOUT = 300
MAX_TOKENS = 8192


@dataclass
class Tool:
    name: str
    description: str
    # JSON Schema của tham số (object)
    schema: dict


@dataclass
class Reply:
    blocks: list[dict]
    usage: dict = field(default_factory=dict)

    @property
    def tool_calls(self) -> list[dict]:
        return [b for b in self.blocks if b["type"] == "tool_use"]


@dataclass(frozen=True)
class Brain:
    """Model đã chọn kèm thông tin gọi API."""

    api: str
    base_url: str
    key: str | None
    model: str


def brain_for(kind: str, base_url: str, key: str | None, model: str) -> Brain:
    spec = ai.PROVIDERS[kind]
    # OpenRouter và các provider tương thích OpenAI dùng chung adapter
    api = spec.api if spec.api in ("anthropic", "gemini") else "openai"
    return Brain(api, base_url.rstrip("/"), key, model)


def _post(url: str, headers: dict[str, str], body: dict, key: str | None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "OpsVibes", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as res:
            return json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        detail = _error_message(e.read().decode(errors="replace"))[:500]
        if key:
            detail = detail.replace(key, "***")
        if e.code == 429:
            raise RateLimitError(_rate_message(detail), _retry_after(detail)) from e
        if e.code in (401, 403):
            raise ai.ProviderError(t("ai.key_rejected", code=e.code)) from e
        raise ai.ProviderError(t("assistant.api_error", code=e.code, detail=detail)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise ai.ProviderError(t("ai.connect_failed", host=urllib.parse.urlsplit(url).netloc, error=reason)) from e
    except ValueError as e:
        raise ai.ProviderError(t("ai.not_json")) from e


def _error_message(body: str) -> str:
    """Lỗi của Gemini/OpenAI/Anthropic đều có dạng {"error": {"message": ...}}: chỉ lấy câu thông báo."""
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return body
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return error if isinstance(error, str) else body


def complete(brain: Brain, system: str, messages: list[dict], tools: list[Tool], max_tokens: int = MAX_TOKENS) -> Reply:
    if brain.api == "anthropic":
        return _anthropic(brain, system, messages, tools, max_tokens)
    if brain.api == "gemini":
        return _gemini(brain, system, messages, tools, max_tokens)
    return _openai(brain, system, messages, tools, max_tokens)


# ---------- Anthropic ----------

def _anthropic(brain: Brain, system: str, messages: list[dict], tools: list[Tool], max_tokens: int) -> Reply:
    def clean(block: dict) -> dict:
        return {k: v for k, v in block.items() if k != "meta"}

    body = {
        "model": brain.model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": m["role"], "content": [clean(b) for b in m["content"]]} for m in messages],
        "tools": [{"name": x.name, "description": x.description, "input_schema": x.schema} for x in tools],
    }
    data = _post(f"{brain.base_url}/messages", {"x-api-key": brain.key or "", "anthropic-version": "2023-06-01"},
                 body, brain.key)
    blocks = []
    for b in data.get("content", []):
        if b.get("type") == "text" and b.get("text"):
            blocks.append({"type": "text", "text": b["text"]})
        elif b.get("type") == "tool_use":
            blocks.append({"type": "tool_use", "id": b["id"], "name": b["name"], "input": b.get("input") or {}})
    usage = data.get("usage") or {}
    return Reply(blocks, {"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)})


# ---------- Tương thích OpenAI (OpenAI, OpenRouter, DeepSeek, Groq, Ollama...) ----------

def _openai(brain: Brain, system: str, messages: list[dict], tools: list[Tool], max_tokens: int) -> Reply:
    out: list[dict] = [{"role": "system", "content": system}]
    for m in messages:
        texts = [b["text"] for b in m["content"] if b["type"] == "text"]
        if m["role"] == "assistant":
            msg: dict = {"role": "assistant", "content": "\n".join(texts) or None}
            calls = [b for b in m["content"] if b["type"] == "tool_use"]
            if calls:
                msg["tool_calls"] = [
                    {"id": b["id"], "type": "function",
                     "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)}}
                    for b in calls
                ]
            out.append(msg)
            continue
        for b in m["content"]:
            if b["type"] == "tool_result":
                out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b["content"]})
        if texts:
            out.append({"role": "user", "content": "\n".join(texts)})

    body = {
        "model": brain.model,
        "messages": out,
        # Model suy luận của OpenAI chỉ nhận max_completion_tokens; chỉ gửi khi cần giới hạn nhỏ (kiểm tra model)
        **({"max_completion_tokens": max_tokens} if max_tokens < MAX_TOKENS else {}),
        "tools": [{"type": "function", "function": {"name": x.name, "description": x.description, "parameters": x.schema}}
                  for x in tools],
    }
    headers = {"Authorization": f"Bearer {brain.key}"} if brain.key else {}
    data = _post(f"{brain.base_url}/chat/completions", headers, body, brain.key)
    choices = data.get("choices") or []
    if not choices:
        raise ai.ProviderError(t("assistant.empty_reply"))
    message = choices[0].get("message") or {}
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        blocks.append({"type": "tool_use", "id": call.get("id") or _new_id(), "name": fn.get("name", ""),
                       "input": args if isinstance(args, dict) else {}})
    usage = data.get("usage") or {}
    return Reply(blocks, {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)})


# ---------- Gemini ----------

def _gemini_schema(schema: dict) -> dict:
    """Gemini chỉ nhận một phần JSON Schema: bỏ các khóa nó không hiểu."""
    allowed = {"type", "description", "properties", "required", "items", "enum"}
    out = {k: v for k, v in schema.items() if k in allowed}
    if "properties" in out:
        out["properties"] = {k: _gemini_schema(v) for k, v in out["properties"].items()}
    if "items" in out:
        out["items"] = _gemini_schema(out["items"])
    return out


def _gemini(brain: Brain, system: str, messages: list[dict], tools: list[Tool], max_tokens: int) -> Reply:
    names = {}  # tool_use_id -> tên tool (functionResponse cần tên, không có id)
    contents = []
    for m in messages:
        parts = []
        for b in m["content"]:
            if b["type"] == "text":
                parts.append({"text": b["text"]})
            elif b["type"] == "tool_use":
                names[b["id"]] = b["name"]
                part: dict = {"functionCall": {"name": b["name"], "args": b["input"]}}
                # Gemini 3 bắt buộc gửi lại chữ ký suy nghĩ kèm functionCall
                signature = (b.get("meta") or {}).get("thought_signature")
                if signature:
                    part["thoughtSignature"] = signature
                parts.append(part)
            elif b["type"] == "tool_result":
                parts.append({"functionResponse": {
                    "name": names.get(b["tool_use_id"], "tool"),
                    "response": {"error" if b.get("is_error") else "result": b["content"]},
                }})
        if parts:
            contents.append({"role": "model" if m["role"] == "assistant" else "user", "parts": parts})

    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": [
            {"name": x.name, "description": x.description, "parameters": _gemini_schema(x.schema)} for x in tools
        ]}],
        "generationConfig": {"maxOutputTokens": max_tokens},
    }
    model = brain.model.removeprefix("models/")
    data = _post(f"{brain.base_url}/models/{urllib.parse.quote(model)}:generateContent",
                 {"x-goog-api-key": brain.key or ""}, body, brain.key)
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise ai.ProviderError(t("assistant.blocked", reason=reason) if reason else t("assistant.empty_reply"))
    blocks: list[dict] = []
    for part in (candidates[0].get("content") or {}).get("parts") or []:
        if part.get("thought"):
            continue
        if part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif "functionCall" in part:
            call = part["functionCall"]
            block = {"type": "tool_use", "id": call.get("id") or _new_id(), "name": call.get("name", ""),
                     "input": call.get("args") or {}}
            if part.get("thoughtSignature"):
                block["meta"] = {"thought_signature": part["thoughtSignature"]}
            blocks.append(block)
    usage = data.get("usageMetadata") or {}
    return Reply(blocks, {"input": usage.get("promptTokenCount", 0), "output": usage.get("candidatesTokenCount", 0)})


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:16]
