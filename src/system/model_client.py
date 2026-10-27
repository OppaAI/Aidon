"""Model client: one interface, any model behind it.

The agent loop never calls a model API directly. It calls
``ModelClient.chat`` with a list of messages, and gets text back.
Behind that interface, the client can be a Meta Model API client
for the demo, a local model server for private use, or a fake
client in tests. That separation is what makes Aidon model-agnostic
by design: swapping the model changes the client, not the loop.

No model call is treated as proof. The loop verifies results
against files and evidence, never against a model's word alone.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Message:
    role: str
    # Text, or a multimodal content list, for vision:
    # [{"type": "text", "text": "..."},
    #  {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]
    # Muse Spark 1.3 is multimodal, text and image input,
    # so an image part is sent as-is, never as a filename
    # alone, which the model cannot see.
    content: str | list = ""


def image_message(text: str, images: list[tuple[bytes, str]]) -> Message:
    """Build a user message with text plus image parts.

    images is a list of (bytes, mime_type), for example
    (png_bytes, "image/png"). Each image becomes a data
    URL, base64, in the OpenAI-compatible image_url part.
    A missing or empty image list is a text message, not
    a broken multimodal message.
    """
    import base64

    if not images:
        return Message("user", text)
    parts: list[dict] = [{"type": "text", "text": text}]
    for data, mime in images:
        if not data:
            continue
        encoded = base64.b64encode(data).decode()
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return Message("user", parts)


def image_file_message(text: str, paths: list) -> Message:
    """Build a user message from image files on disk.

    Mime type is guessed from the suffix, png, jpeg, gif,
    webp, otherwise image/png. A missing file is an error,
    never a silent text-only message.
    """
    import mimetypes
    from pathlib import Path

    images = []
    for path in paths:
        data = Path(path).read_bytes()
        mime, _ = mimetypes.guess_type(str(path))
        images.append((data, mime or "image/png"))
    return image_message(text, images)


class ModelClient:
    """Base interface. Subclasses implement one chat call."""

    def chat(self, messages: list[Message]) -> str:
        raise NotImplementedError


class FakeClient(ModelClient):
    """Test client: returns a canned reply, makes no network call."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.calls: list[list[Message]] = []

    def chat(self, messages: list[Message]) -> str:
        self.calls.append(list(messages))
        return self.reply


class HttpModelClient(ModelClient):
    """HTTP client for an OpenAI-compatible chat endpoint.

    Used for the Meta Model API in the demo and for a local model
    server in private use. The endpoint and key come from the
    configuration, never from code, and never from chat.
    """

    def __init__(self, api_base: str, api_key: str, model: str,
                 timeout: float = 120.0):
        if not api_base:
            raise ValueError("api_base is required for HttpModelClient")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def chat(self, messages: list[Message]) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content}
                         for m in messages],
        }
        # A list content is sent as-is, so image parts reach
        # the model, not just the text part. A string content
        # is unchanged, so old text-only calls keep working.
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content
                           if isinstance(part, dict))
        return str(content)


class OllamaClient(ModelClient):
    """Local Ollama, native ``/api/chat``, stdlib only.

    The user's ``.env`` points AIDON_API_ENDPOINT at
    ``http://localhost:11434`` (no ``/v1``), which is Ollama's
    native API, not an OpenAI-compatible one. Posting to
    ``/chat/completions`` there returns 404, so a generic HTTP
    client cannot drive it. This client speaks the native API
    and maps the two knobs that matter for latency and memory:
    ``think`` (chain-of-thought, off by default here because it
    multiplies output tokens) and ``num_ctx`` (Ollama silently
    truncates to a small default when this is unset).
    """

    def __init__(self, endpoint: str, model: str, timeout: float = 120.0,
                 think: bool = False, num_ctx: int = 0):
        self.base = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.think = think
        self.num_ctx = num_ctx

    def _body(self, messages: list[Message]) -> dict:
        converted = []
        for message in messages:
            content = message.content
            images: list[str] = []
            if isinstance(content, list):
                # OpenAI-style multimodal parts -> Ollama text + images.
                text_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        text_parts.append(str(part.get("text", "")))
                    elif part.get("type") == "image_url":
                        url = str((part.get("image_url") or {}).get("url", ""))
                        if ";base64," in url:
                            images.append(url.split(";base64,", 1)[1])
                content = "\n".join(text_parts)
            item = {"role": message.role, "content": content}
            if images:
                item["images"] = images
            converted.append(item)
        body: dict = {"model": self.model, "messages": converted,
                      "stream": False, "think": self.think}
        if self.num_ctx:
            body["options"] = {"num_ctx": self.num_ctx}
        return body

    def chat(self, messages: list[Message]) -> str:
        body = self._body(messages)
        req = urllib.request.Request(
            f"{self.base}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Ollama {self.base} returned {exc.code}: {detail[:300]}"
            ) from exc
        message = data.get("message") or {}
        return str(message.get("content", "") or "")


class MetaModelAPIClient(HttpModelClient):
    """Meta Model API (Muse Spark 1.3), OpenAI-compatible."""

    DEFAULT_ENDPOINT = "https://api.meta.ai/v1"

    def __init__(self, api_key: str, model: str,
                 endpoint: str = "", timeout: float = 120.0):
        if not api_key:
            raise ValueError(
                "AIDON_META_API_KEY (or AIDON_API_KEY) is required "
                "for the Meta Model API backend")
        super().__init__(endpoint or self.DEFAULT_ENDPOINT, api_key, model,
                         timeout=timeout)


def client_for_config(config) -> ModelClient:
    """Build the model client the ``.env`` asks for.

    Backend is chosen by ``AIDON_BACKEND`` (ollama|meta|http|fake),
    and inferred when it is unset: a Meta key means meta, a
    non-Ollama endpoint means http, anything else is fake so an
    offline checkout still runs its tests.
    """
    backend = (getattr(config, "backend", "") or "").strip().lower()
    api_base = (getattr(config, "api_base", "") or "").strip()
    if not backend:
        if getattr(config, "meta_api_key", ""):
            backend = "meta"
        elif api_base and "localhost:11434" not in api_base \
                and "127.0.0.1:11434" not in api_base:
            backend = "http"
        elif api_base:
            backend = "ollama"
        else:
            backend = "fake"
    timeout = float(getattr(config, "model_timeout_seconds", 120.0) or 120.0)
    if backend == "fake":
        return FakeClient("")
    if backend == "ollama":
        endpoint = api_base or getattr(
            config, "ollama_endpoint", "http://localhost:11434")
        return OllamaClient(
            endpoint, config.model, timeout=timeout,
            think=bool(getattr(config, "ollama_think", False)),
            num_ctx=int(getattr(config, "ollama_num_ctx", 0) or 0))
    if backend == "meta":
        return MetaModelAPIClient(
            getattr(config, "meta_api_key", "") or config.api_key,
            getattr(config, "meta_model", "") or config.model,
            endpoint=getattr(config, "meta_endpoint", ""),
            timeout=timeout)
    if backend == "http":
        return HttpModelClient(api_base, config.api_key, config.model,
                               timeout=timeout)
    raise ValueError(
        f"Unknown AIDON_BACKEND {backend!r} "
        "(expected ollama, meta, http, or fake)")
