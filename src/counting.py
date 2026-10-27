"""Comprehensive model-usage counting, not just plan tokens.

The old budget counted tokens around _plan() only. That does
not represent all inference: repair, verification, adaptation,
swarm, and interview calls are model calls too, and a benchmark
that only counts plan tokens understates the total and can
claim savings it did not measure.

CountingClient wraps any ModelClient, counts every chat call,
and estimates tokens at one token per four characters of
input plus output, unless the wrapped client reports a real
usage count, in which case that replaces the estimate. Calls
are grouped by phase when the caller passes a phase, so a
report can say: plan 1 call, repair 0, verify 0, adapt 1,
not just a total. A report labels the token count as an
estimate, never as an exact model token count, until a real
usage count is available from the model API.
"""

from __future__ import annotations

from .model_client import ModelClient


def _text_of(messages) -> str:
    parts = []
    for message in messages:
        content = message.content
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text", "")))
        else:
            parts.append(str(content))
    return "\n".join(parts)


class CountingClient(ModelClient):
    """Wrap a client, count every call, by phase."""

    def __init__(self, inner: ModelClient):
        self.inner = inner
        self.calls = 0
        self.tokens_estimated = 0
        self.calls_by_phase: dict[str, int] = {}
        self.tokens_by_phase: dict[str, int] = {}

    def chat(self, messages, phase: str = "chat") -> str:
        reply = self.inner.chat(messages)
        tokens = max(1, (len(_text_of(messages)) + len(reply)) // 4)
        self.calls += 1
        self.tokens_estimated += tokens
        self.calls_by_phase[phase] = self.calls_by_phase.get(phase, 0) + 1
        self.tokens_by_phase[phase] = self.tokens_by_phase.get(phase, 0) + tokens
        return reply

    def chat_phase(self, phase: str, messages) -> str:
        return self.chat(messages, phase=phase)

    def report(self) -> dict:
        return {"model_calls": self.calls,
                "model_tokens_estimated": self.tokens_estimated,
                "calls_by_phase": dict(self.calls_by_phase),
                "tokens_by_phase": dict(self.tokens_by_phase)}
