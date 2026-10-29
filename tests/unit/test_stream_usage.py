"""Streamed real token usage.

When a streamed response carries provider usage, the footer basis
is "actual" with the provider's own counts, exactly like a
non-streamed call; when it does not, the chars//4 estimate stands.
Accuracy is the provider's own number (100% for completed streams)
and latency is unchanged: the usage chunk rides inside the stream,
before [DONE], with no extra request.
"""

import json

from src.agentic.counting import CountingClient
from src.system.model_client import HttpModelClient, Message, NativeModelClient
from src.system.model_router import RouterClient


class _StreamResponse:
    """Iterable SSE/NDJSON stand-in with the release hooks _release needs."""

    status = 200
    reason = "OK"
    headers = {}
    will_close = True

    def __init__(self, lines, status=200):
        self._lines = lines
        self.status = status

    def __iter__(self):
        return iter(self._lines)

    def read(self):
        return b"{}"

    def isclosed(self):
        return True


def _sse(*chunks):
    lines = []
    for chunk in chunks:
        lines.append(("data: " + json.dumps(chunk)).encode())
    lines.append(b"data: [DONE]")
    return lines


def _msgs():
    return [Message("user", "hello world, this is a test message")]


def test_http_stream_captures_usage_chunk(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = json.loads(body)
        return _StreamResponse(_sse(
            {"choices": [{"delta": {"content": "hello "}}]},
            {"choices": [{"delta": {"content": "world"}}]},
            {"choices": [],
             "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
        )), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = HttpModelClient("http://model/v1", "key", "test")
    text = "".join(client.chat_stream(_msgs()))
    assert text == "hello world"
    assert seen["body"]["stream_options"] == {"include_usage": True}
    assert client.last_stream_usage == {"prompt_tokens": 10,
                                        "completion_tokens": 5}


def test_http_stream_without_usage_chunk_stays_estimated(monkeypatch):
    def fake_post(url, body, headers, timeout):
        return _StreamResponse(_sse(
            {"choices": [{"delta": {"content": "hi"}}]},
        )), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = HttpModelClient("http://model/v1", "key", "test")
    assert "".join(client.chat_stream(_msgs())) == "hi"
    assert client.last_stream_usage is None


def test_http_stream_retries_without_stream_options_on_400(monkeypatch):
    calls = []

    def fake_post(url, body, headers, timeout):
        payload = json.loads(body)
        calls.append(payload)
        if len(calls) == 1:
            return _StreamResponse([], status=400), ("http", "fake", 80)
        return _StreamResponse(_sse(
            {"choices": [{"delta": {"content": "ok"}}]},
        )), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = HttpModelClient("http://model/v1", "key", "test")
    assert "".join(client.chat_stream(_msgs())) == "ok"
    assert calls[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in calls[1]
    assert client.last_stream_usage is None


def test_native_stream_captures_eval_counts(monkeypatch):
    def fake_post(url, body, headers, timeout):
        lines = [
            json.dumps({"message": {"content": "yo"},
                        "done": False}).encode(),
            json.dumps({"message": {"content": ""}, "done": True,
                        "prompt_eval_count": 7, "eval_count": 3}).encode(),
        ]
        return _StreamResponse(lines), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = NativeModelClient("http://localhost:11434", "test")
    assert "".join(client.chat_stream(_msgs())) == "yo"
    assert client.last_stream_usage == {"prompt_tokens": 7,
                                        "completion_tokens": 3}


class _UsageStreamClient:
    def __init__(self, usage=None):
        self.last_stream_usage = usage

    def chat_stream(self, messages, sampling=None):
        yield "streamed reply"


def test_counting_stream_uses_provider_usage():
    usage = {"prompt_tokens": 40, "completion_tokens": 20,
             "completion_tokens_details": {"reasoning_tokens": 5}}
    client = CountingClient(_UsageStreamClient(usage))
    assert "".join(client.chat_stream(_msgs())) == "streamed reply"
    assert client.in_tokens_actual == 40
    assert client.out_tokens_actual == 20
    assert client.calls_with_usage == 1
    assert client.thinking_tokens_total == 5
    assert client.report()["model_tokens_basis"] == "actual"


def test_counting_stream_without_usage_stays_estimated():
    client = CountingClient(_UsageStreamClient(None))
    list(client.chat_stream(_msgs()))
    assert client.calls_with_usage == 0
    assert client.report()["model_tokens_basis"] == "estimated"


def test_router_stream_surfaces_tier_usage():
    usage = {"prompt_tokens": 40, "completion_tokens": 20}
    tier = CountingClient(_UsageStreamClient(usage))
    router = RouterClient(clients={"respond": tier}, default=tier)
    assert "".join(router.chat_stream("respond", _msgs())) == "streamed reply"
    assert router.last_stream_usage == usage
