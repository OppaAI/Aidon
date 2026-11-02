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

import http.client
import io
import json
import ssl
import threading
import uuid
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class _KeepAlive:
    """Per-thread keep-alive connections, one per origin.

    A model call is one POST, and a run makes many of them. Without
    pooling every call pays a fresh TCP handshake and, over https, a
    full TLS handshake, measured at ~55ms per call to the Meta
    endpoint. Reusing the connection removes that cost for every call
    after the first.

    One connection per (thread, origin): the WebUI is threaded, and a
    connection must never be shared across threads mid-request.
    Connections are never shared across processes either, so this
    holds nothing that a forked child could corrupt.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    def _conns(self) -> dict:
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = {}
            self._local.conns = conns
        return conns

    def get(self, origin: tuple, timeout: float):
        conn = self._conns().get(origin)
        if conn is not None:
            return conn
        scheme, host, port = origin
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port, timeout=timeout,
                context=ssl.create_default_context())
        else:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
        self._conns()[origin] = conn
        return conn

    def drop(self, origin: tuple) -> None:
        conn = self._conns().pop(origin, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_KEEPALIVE = _KeepAlive()

# Stale keep-alive sockets fail like this when the peer closed them
# between calls. They are retried once on a fresh connection; anything
# else (DNS, TLS, auth) propagates at once instead of being retried.
_STALE = (http.client.RemoteDisconnected, http.client.CannotSendRequest,
          http.client.BadStatusLine, ConnectionResetError, BrokenPipeError)


def _origin(url: str) -> tuple:
    """(scheme, host, port) and the request path for a URL."""
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if scheme == "https" else 80)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return (scheme, host, port), path


def _post(url: str, body: bytes, headers: dict, timeout: float):
    """POST over a pooled connection, retrying one stale socket.

    Returns (response, origin); the caller must read the response and
    then call ``_release`` so a half-read body is never reused.
    """
    origin, path = _origin(url)
    for attempt in (0, 1):
        conn = _KEEPALIVE.get(origin, timeout)
        try:
            conn.timeout = timeout
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            conn.request("POST", path, body=body, headers=headers)
            return conn.getresponse(), origin
        except _STALE:
            _KEEPALIVE.drop(origin)
            if attempt:
                raise
    raise RuntimeError("unreachable")


def _release(resp, origin: tuple) -> None:
    """Keep the connection only if the whole body was read and the
    peer did not ask to close it."""
    if resp.will_close or not resp.isclosed():
        _KEEPALIVE.drop(origin)


def _read_json(resp, origin: tuple, url: str) -> dict:
    """Read a JSON body, mapping HTTP errors onto urllib's HTTPError
    so existing callers keep their error handling."""
    try:
        if resp.status >= 400:
            data = resp.read()
            raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                         resp.headers, io.BytesIO(data))
        return json.loads(resp.read().decode("utf-8"))
    finally:
        _release(resp, origin)


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


@dataclass
class ToolCall:
    """Compatibility representation for native model tool calls."""
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:8]}")


@dataclass
class ChatResponse:
    """Normalized response shape for integrations that expose tool calls."""
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    thinking: str = ""
    raw: Any = None


def parse_native_tool_calls(message: dict) -> list[ToolCall]:
    calls = []
    for item in message.get("tool_calls") or []:
        function = item.get("function", {}) if isinstance(item, dict) else {}
        args = function.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args}
        calls.append(ToolCall(str(function.get("name", "")),
                              args if isinstance(args, dict) else {"_raw": str(args)}))
    return calls


def parse_openai_tool_calls(message: dict) -> list[ToolCall]:
    calls = []
    for item in message.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function", {})
        raw = function.get("arguments", "{}")
        try:
            args = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        except (json.JSONDecodeError, TypeError):
            args = {"_raw": str(raw)}
        calls.append(ToolCall(str(function.get("name", "")),
                              args if isinstance(args, dict) else {"_raw": str(args)},
                              str(item.get("id", "")) or f"call_{uuid.uuid4().hex[:8]}"))
    return calls


def estimate_tokens(text: str) -> int:
    return max(1, len(str(text)) // 4)


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

    def chat(self, messages: list[Message], sampling=None) -> str:
        raise NotImplementedError

    def chat_stream(self, messages: list[Message], sampling=None):
        """Yield text deltas when supported; default keeps old clients safe."""
        yield self.chat(messages, sampling=sampling)

    def chat_response(self, messages: list[Message], sampling=None,
                      tools=None) -> ChatResponse:
        """Compatibility adapter for callers expecting a normalized response."""
        return ChatResponse(text=self.chat(messages, sampling=sampling))


class FakeClient(ModelClient):
    """Test client: returns a canned reply, makes no network call."""

    def __init__(self, reply: str = ""):
        self.reply = reply
        self.calls: list[list[Message]] = []

    def chat(self, messages: list[Message], sampling=None) -> str:
        self.calls.append(list(messages))
        self.last_sampling = sampling
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
        self.last_stream_usage = None

    def _payload(self, messages: list[Message], sampling=None, tools=None) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content}
                         for m in messages],
        }
        if sampling:
            if sampling.get("temperature") is not None:
                payload["temperature"] = sampling["temperature"]
            if sampling.get("top_p") is not None:
                payload["top_p"] = sampling["top_p"]
            if sampling.get("seed") is not None:
                payload["seed"] = sampling["seed"]
            if sampling.get("reasoning_effort") is not None:
                # Meta chat completions: "none" is rejected by Muse
                # Spark. "max" is the standard model's top level but
                # is NOT supported by the Contributor variant, which
                # tops at "xhigh" — downgrade there so a /think run
                # on the test model does not 400.
                effort = str(sampling["reasoning_effort"])
                if effort == "max" and "contributor" in str(self.model).lower():
                    effort = "xhigh"
                payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = tools
        return payload

    def _post(self, payload: dict) -> dict:
        # A list content is sent as-is, so image parts reach
        # the model, not just the text part. A string content
        # is unchanged, so old text-only calls keep working.
        url = f"{self.api_base}/chat/completions"
        resp, origin = _post(
            url,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        return _read_json(resp, origin, url)

    @staticmethod
    def _content(message: dict) -> str:
        content = message.get("content", "")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content
                           if isinstance(part, dict))
        return str(content)

    def chat_response(self, messages: list[Message], sampling=None,
                      tools=None) -> ChatResponse:
        data = self._post(self._payload(messages, sampling=sampling, tools=tools))
        choices = data.get("choices") or []
        if not choices:
            return ChatResponse(raw=data)
        message = choices[0].get("message", {}) or {}
        return ChatResponse(
            text=self._content(message),
            tool_calls=parse_openai_tool_calls(message),
            usage=data.get("usage") or {}, raw=data)

    def chat(self, messages: list[Message], sampling=None) -> str:
        return self.chat_response(messages, sampling=sampling).text

    def chat_stream(self, messages: list[Message], sampling=None):
        payload = self._payload(messages, sampling=sampling)
        payload["stream"] = True
        # Chat Completions usage chunk: the provider sends one final
        # chunk with an empty choices array and the whole-request
        # usage, before [DONE]. Asking for it costs no extra request
        # and no extra latency; when the provider does not send it,
        # the caller falls back to the estimate, as before.
        payload["stream_options"] = {"include_usage": True}
        self.last_stream_usage = None
        try:
            yield from self._stream_payload(messages, sampling, payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                raise
            # A provider that rejects stream_options still streams
            # fine without it. The 400 surfaces before any chunk is
            # yielded, so retrying once cannot duplicate text; usage
            # then falls back to the estimate.
            payload = dict(payload)
            payload.pop("stream_options", None)
            self.last_stream_usage = None
            yield from self._stream_payload(messages, sampling, payload)

    def _stream_payload(self, messages, sampling, payload):
        url = f"{self.api_base}/chat/completions"
        resp, origin = _post(
            url,
            json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json",
             "Authorization": f"Bearer {self.api_key}"},
            self.timeout,
        )
        emitted = False
        try:
            if resp.status >= 400:
                data = resp.read()
                raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                             resp.headers, io.BytesIO(data))
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                usage = data.get("usage")
                if usage:
                    self.last_stream_usage = usage
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content", "")
                if text:
                    emitted = True
                    yield str(text)
        finally:
            _release(resp, origin)
        if not emitted:
            # Some compatible gateways ignore stream=true and return one
            # normal JSON response. Preserve compatibility rather than
            # silently showing an empty answer.
            yield self.chat(messages, sampling=sampling)


class NativeModelClient(ModelClient):
    """Native ``/api/chat`` client, stdlib only.

    This protocol is not OpenAI-compatible. Posting to
    ``/chat/completions`` returns 404, so a generic HTTP client
    cannot drive it. This client speaks the native API
    and maps the two knobs that matter for latency and memory:
    ``think`` (chain-of-thought, off by default here because it
    multiplies output tokens) and ``num_ctx`` (the server silently
    truncates to a small default when this is unset).
    """

    def __init__(self, endpoint: str, model: str, timeout: float = 120.0,
                 think: bool = False, num_ctx: int = 0):
        self.base = endpoint.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.think = think
        self.num_ctx = num_ctx
        self.last_stream_usage = None

    def _body(self, messages: list[Message]) -> dict:
        converted = []
        for message in messages:
            content = message.content
            images: list[str] = []
            if isinstance(content, list):
                # OpenAI-style multimodal parts -> native text + images.
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

    def _post_chat(self, body: dict):
        """Post a native chat request, retrying unsupported thinking once.

        Ollama returns HTTP 400 when ``think: true`` is sent to a model
        that does not expose thinking. Aidon's phase effort is advisory,
        so preserve the request and retry without that optional hint. Other
        errors remain visible to the caller.
        """
        url = f"{self.base}/api/chat"
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        resp, origin = _post(url, payload, headers, self.timeout)
        if resp.status < 400:
            return resp, origin

        data = resp.read()
        status = resp.status
        _release(resp, origin)
        detail = data.decode("utf-8", "replace")
        if (body.get("think") is True and
                "does not support thinking" in detail.lower()):
            retry_body = dict(body)
            retry_body["think"] = False
            return _post(url, json.dumps(retry_body).encode("utf-8"),
                         headers, self.timeout)
        raise RuntimeError(
            f"Native model endpoint {self.base} returned {status}: {detail[:300]}"
        )

    def _request(self, body: dict) -> dict:
        url = f"{self.base}/api/chat"
        resp, origin = self._post_chat(body)
        try:
            return _read_json(resp, origin, url)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                pass
            raise RuntimeError(
                f"Native model endpoint {self.base} returned {exc.code}: {detail[:300]}"
            ) from exc

    def chat_response(self, messages: list[Message], sampling=None,
                      tools=None) -> ChatResponse:
        body = self._body(messages)
        if tools:
            body["tools"] = tools
        if sampling:
            if sampling.get("think") is not None:
                body["think"] = bool(sampling["think"])
            body.setdefault("options", {})
            if sampling.get("temperature") is not None:
                body["options"]["temperature"] = sampling["temperature"]
            if sampling.get("top_p") is not None:
                body["options"]["top_p"] = sampling["top_p"]
            if sampling.get("seed") is not None:
                body["options"]["seed"] = sampling["seed"]
        data = self._request(body)
        message = data.get("message") or {}
        return ChatResponse(
            text=str(message.get("content", "") or ""),
            tool_calls=parse_native_tool_calls(message),
            usage=data.get("prompt_eval_count") and {
                "prompt_eval_count": data.get("prompt_eval_count"),
                "eval_count": data.get("eval_count"),
            } or {},
            thinking=str(message.get("thinking", "") or ""), raw=data)

    def chat(self, messages: list[Message], sampling=None) -> str:
        return self.chat_response(messages, sampling=sampling).text

    def chat_stream(self, messages: list[Message], sampling=None):
        body = self._body(messages)
        body["stream"] = True
        self.last_stream_usage = None
        if sampling:
            if sampling.get("think") is not None:
                body["think"] = bool(sampling["think"])
            body.setdefault("options", {})
            for key in ("temperature", "top_p", "seed"):
                if sampling.get(key) is not None:
                    body["options"][key] = sampling[key]
        url = f"{self.base}/api/chat"
        resp, origin = self._post_chat(body)
        emitted = False
        try:
            if resp.status >= 400:
                data = resp.read()
                raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                             resp.headers, io.BytesIO(data))
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("done") and (data.get("prompt_eval_count") is not None
                                         or data.get("eval_count") is not None):
                    self.last_stream_usage = {
                        "prompt_tokens": data.get("prompt_eval_count") or 0,
                        "completion_tokens": data.get("eval_count") or 0}
                message = data.get("message") or {}
                text = message.get("content", "")
                if text:
                    emitted = True
                    yield str(text)
        finally:
            _release(resp, origin)
        if not emitted:
            yield self.chat(messages, sampling=sampling)


class MetaModelAPIClient(HttpModelClient):
    """Meta Model API (Muse Spark 1.3), OpenAI-compatible."""

    DEFAULT_ENDPOINT = "https://api.meta.ai/v1"

    def __init__(self, api_key: str, model: str,
                 endpoint: str = "", timeout: float = 120.0):
        if not api_key:
            raise ValueError(
                "AIDON_API_KEY is required for the Meta Model API backend")
        super().__init__(endpoint or self.DEFAULT_ENDPOINT, api_key, model,
                         timeout=timeout)


def client_for_config(config) -> ModelClient:
    """Build the model client the ``.env`` asks for.

    Backend is chosen by ``AIDON_BACKEND`` (native|meta|http|fake),
    and inferred when it is unset: a Meta key means meta, a
    non-native endpoint means http, anything else is fake so an
    offline checkout still runs its tests.
    """
    backend = (getattr(config, "backend", "") or "").strip().lower()
    api_base = (getattr(config, "api_base", "") or "").strip()
    if backend in ("", "auto"):
        # Old keys only: AIDON_API_KEY + AIDON_API_ENDPOINT.
        # A Meta endpoint means meta, a local native endpoint means
        # native, any other endpoint means http, else fake.
        if api_base and "api.meta.ai" in api_base:
            backend = "meta"
        elif api_base and ("localhost:11434" in api_base
                           or "127.0.0.1:11434" in api_base):
            backend = "native"
        elif api_base:
            backend = "http"
        elif getattr(config, "api_key", ""):
            backend = "meta"
        else:
            backend = "fake"
    timeout = float(getattr(config, "model_timeout_seconds", 120.0) or 120.0)
    if backend == "fake":
        return FakeClient("")
    if backend == "native":
        endpoint = api_base or getattr(
            config, "native_endpoint", "http://localhost:11434")
        return NativeModelClient(
            endpoint, config.model, timeout=timeout,
            think=bool(getattr(config, "model_think", False)),
            num_ctx=int(getattr(config, "model_num_ctx", 0) or 0))
    if backend == "meta":
        return MetaModelAPIClient(
            config.api_key, config.model,
            endpoint=api_base,
            timeout=timeout)
    if backend == "http":
        return HttpModelClient(api_base, config.api_key, config.model,
                               timeout=timeout)
    raise ValueError(
        f"Unknown AIDON_BACKEND {backend!r} "
        "(expected native, meta, http, or fake)")
