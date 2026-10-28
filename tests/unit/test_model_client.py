"""Native structured tool-call handling for model clients."""

import json

from src.system.model_client import HttpModelClient, Message, OllamaClient


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.payload


def test_ollama_native_tool_call_is_normalized(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
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
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    tool_schema = [{"type": "function", "function": {
        "name": "read_file", "parameters": {"type": "object"}
    }}]
    response = OllamaClient("http://localhost:11434", "test").chat_response(
        [Message("user", "read note.md")], tools=tool_schema)

    assert seen["url"].endswith("/api/chat")
    assert seen["body"]["tools"] == tool_schema
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"file": "note.md"}
    assert response.thinking == "brief"


def test_openai_compatible_tool_call_is_normalized(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout):
        seen["body"] = json.loads(request.data)
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
        })

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    response = HttpModelClient("http://model/v1", "key", "test").chat_response(
        [Message("user", "write x.txt")], tools=[{"type": "function"}])

    assert seen["body"]["tools"] == [{"type": "function"}]
    assert response.text == "done"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].arguments["text"] == "ok"
    assert response.usage["total_tokens"] == 4
