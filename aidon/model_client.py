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
import urllib.request
from dataclasses import dataclass


@dataclass
class Message:
    role: str
    content: str


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

    def __init__(self, api_base: str, api_key: str, model: str):
        if not api_base:
            raise ValueError("api_base is required for HttpModelClient")
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key
        self.model = model

    def chat(self, messages: list[Message]) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content}
                         for m in messages],
        }
        req = urllib.request.Request(
            f"{self.api_base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choices = data.get("choices") or []
        if not choices:
            return ""
        content = choices[0].get("message", {}).get("content", "")
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content
                           if isinstance(part, dict))
        return str(content)
