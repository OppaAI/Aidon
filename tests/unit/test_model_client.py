"""Native structured tool-call handling for model clients."""

import json

from src.system.model_client import HttpModelClient, Message, NativeModelClient


class _Response:
    """Minimal stand-in for the pooled http.client response.

    The client reads ``status``/``read()`` and releases the
    connection through ``will_close``/``isclosed()``, so a fake
    only needs those four.
    """

    status = 200
    will_close = True

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def read(self):
        return self.payload

    def isclosed(self):
        return True


def test_native_native_tool_call_is_normalized(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["url"] = url
        seen["body"] = json.loads(body)
        return _Response({
            "message": {
                "role": "assistant",
                "content": "",
                "thinking": "brief",
                "tool_calls": [{"function": {
                    "name": "read_file",
                    "arguments": {"file": "note.md"},
                }}],
            },
            "eval_count": 12,
        }), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    tool_schema = [{"type": "function", "function": {
        "name": "read_file", "parameters": {"type": "object"}
    }}]
    response = NativeModelClient("http://localhost:11434", "test").chat_response(
        [Message("user", "read note.md")], tools=tool_schema)

    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["tools"] == tool_schema
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"file": "note.md"}
    assert response.thinking == "brief"


def test_native_retries_without_thinking_when_model_rejects_it(monkeypatch):
    requests = []

    class _ErrorResponse(_Response):
        status = 400
        reason = "Bad Request"
        headers = {}

        def __init__(self):
            self.payload = b'{"error":"\\"test-model\\" does not support thinking"}'

    def fake_post(url, body, headers, timeout):
        requests.append(json.loads(body))
        if len(requests) == 1:
            return _ErrorResponse(), ("http", "fake", 80)
        return _Response({"message": {"content": "pwd is /workspace"}}), (
            "http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = NativeModelClient("http://localhost:11434", "test-model")
    response = client.chat_response(
        [Message("user", "Run pwd")], sampling={"think": True})

    assert response.text == "pwd is /workspace"
    assert requests[0]["think"] is True
    assert requests[1]["think"] is False


def test_native_does_not_retry_other_http_errors(monkeypatch):
    class _ErrorResponse(_Response):
        status = 400
        reason = "Bad Request"
        headers = {}

        def __init__(self):
            self.payload = b'{"error":"invalid model"}'

    calls = []

    def fake_post(url, body, headers, timeout):
        calls.append(json.loads(body))
        return _ErrorResponse(), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    client = NativeModelClient("http://localhost:11434", "test-model")
    try:
        client.chat_response([Message("user", "hello")],
                             sampling={"think": True})
    except RuntimeError as exc:
        assert "invalid model" in str(exc)
    else:
        raise AssertionError("expected invalid model error")

    assert len(calls) == 1


def test_openai_compatible_tool_call_is_normalized(monkeypatch):
    seen = {}

    def fake_post(url, body, headers, timeout):
        seen["body"] = json.loads(body)
        return _Response({
            "choices": [{"message": {
                "content": "done",
                "tool_calls": [{
                    "id": "call-1",
                    "function": {"name": "write_file",
                                 "arguments": '{"file":"x.txt","text":"ok"}'},
                }],
            }}],
            "usage": {"total_tokens": 4},
        }), ("http", "fake", 80)

    monkeypatch.setattr("src.system.model_client._post", fake_post)
    response = HttpModelClient("http://model/v1", "key", "test").chat_response(
        [Message("user", "write x.txt")], tools=[{"type": "function"}])

    assert seen["body"]["tools"] == [{"type": "function"}]
    assert response.text == "done"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].arguments["text"] == "ok"
    assert response.usage["total_tokens"] == 4
