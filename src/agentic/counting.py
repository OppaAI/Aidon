"""Comprehensive model-usage counting, not just plan tokens.

Counting tokens only around _plan() is incomplete. That does
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
not just a total. Real and estimated counts are tracked
separately, never added into one number: in_tokens_actual /
out_tokens_actual hold provider-reported usage only, while
in_tokens_estimated / out_tokens_estimated hold the
chars//4 split. A finished-run total uses the real counts
when every call reported real usage, and the estimate
otherwise, and says which basis it is — so a footer never
silently mixes an estimate with a real count, and trace
rows (real when present, estimate otherwise) agree with
the footer whenever the basis is actual.

Per-step display (the right-bar trace) reads the same data
through take_phase_usage(): real usage when the backend
reported it — prompt_tokens in, completion_tokens out,
completion_tokens_details.reasoning_tokens as thinking
tokens — with a chars//4 split estimate when it did not.
take_phase_usage drains the
phase's delta without touching the cumulative totals, so the
finished-message total never changes when a trace event
reads its step's tokens. Streamed calls have no usage to
read (server-sent deltas carry text only, and the stream
protocol surfaces no usage chunk), so they stay estimated,
thinking tokens 0 — capturing real streamed usage needs
the stream protocol to carry it, which is a separate change.
Thinking tokens are real-only by nature: thinking text
never appears in a reply, so no chars//4 estimate exists
for it, and it is added to a total only as a real count.
"""

from __future__ import annotations

from ..system.model_client import ModelClient


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
        import threading
        self.inner = inner
        self.calls = 0
        self.tokens_estimated = 0
        self.calls_by_phase: dict[str, int] = {}
        self.tokens_by_phase: dict[str, int] = {}
        # Per-phase usage deltas for the trace display. take()
        # drains them; the cumulative totals above are untouched.
        self._usage_in: dict[str, int] = {}
        self._usage_out: dict[str, int] = {}
        self._usage_thinking: dict[str, int] = {}
        # Cumulative thinking tokens, never drained: the per-phase
        # deltas above are popped by take_phase_usage for trace
        # events, but the finished-run total still needs every
        # thinking token the run spent.
        self.thinking_tokens_total: int = 0
        # Cumulative estimate split: the chars//4 estimate is one
        # number per call, but the footer breaks it into in/out.
        # Split proportionally to prompt/reply text so in + out
        # always equals tokens_estimated exactly.
        self.in_tokens_estimated: int = 0
        self.out_tokens_estimated: int = 0
        # Cumulative REAL usage, provider-reported only: calls
        # without usage contribute nothing here, so these
        # counters never mix an estimate with a real count.
        # calls_with_usage tells the footer whether the real
        # counters cover the whole run (basis "actual") or
        # only part of it (basis "estimated").
        self.in_tokens_actual: int = 0
        self.out_tokens_actual: int = 0
        self.calls_with_usage: int = 0
        self._thinking_text: dict[str, str] = {}
        # Parallel LLM steps count through here concurrently;
        # without a lock, increments from two threads can
        # interleave and silently lose counts.
        self._lock = threading.Lock()

    def _split_estimate(self, messages, reply: str) -> tuple[int, int]:
        """Chars//4 split estimate, the fallback rule."""
        return (max(1, len(_text_of(messages)) // 4),
                max(1, len(reply) // 4))

    def _record_usage(self, phase: str, messages, reply: str,
                      usage: dict | None, thinking_text: str = "") -> None:
        """Fold one call's tokens into the phase's delta."""
        usage = usage or {}
        details = usage.get("completion_tokens_details") or {}
        if usage.get("prompt_tokens") is not None or \
                usage.get("completion_tokens") is not None:
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            thinking = int(details.get("reasoning_tokens") or 0)
            self.in_tokens_actual += tokens_in
            self.out_tokens_actual += tokens_out
            self.calls_with_usage += 1
        else:
            tokens_in, tokens_out = self._split_estimate(messages, reply)
            thinking = 0
        self._usage_in[phase] = self._usage_in.get(phase, 0) + tokens_in
        self._usage_out[phase] = self._usage_out.get(phase, 0) + tokens_out
        self._usage_thinking[phase] = \
            self._usage_thinking.get(phase, 0) + thinking
        self.thinking_tokens_total += thinking
        if thinking_text:
            self._thinking_text[phase] = thinking_text

    def take_phase_usage(self, phase: str) -> dict:
        """Drain one phase's usage delta for a trace event.

        Returns tokens_in, tokens_out, thinking_tokens, and the
        last thinking text for the phase ("" when the backend
        redacts it, as Meta chat completions does).
        """
        with self._lock:
            result = {"tokens_in": self._usage_in.pop(phase, 0),
                      "tokens_out": self._usage_out.pop(phase, 0),
                      "thinking_tokens": self._usage_thinking.pop(phase, 0),
                      "thinking": self._thinking_text.pop(phase, "")}
        return result

    def _call_with_usage(self, messages, phase: str, sampling=None):
        """One call, preferring chat_response so usage survives.

        Returns (reply_text, response) on success, (None, False)
        only when the inner client has no usable chat_response.
        A real failure inside chat_response propagates exactly
        like a failure inside chat used to — it must never
        silently retry the call through a second method.
        """
        chat_response = getattr(self.inner, "chat_response", None)
        if not callable(chat_response):
            return None, False
        try:
            response = chat_response(messages, sampling=sampling)
        except TypeError:
            response = chat_response(messages)
        text = getattr(response, "text", None)
        if not isinstance(text, str):
            return None, False
        return text, response

    def chat(self, messages, phase: str = "chat", sampling=None) -> str:
        reply, response = self._call_with_usage(messages, phase, sampling)
        if response is False:
            try:
                reply = self.inner.chat(messages, sampling=sampling)
            except TypeError:
                reply = self.inner.chat(messages)
            usage, thinking_text = None, ""
        else:
            usage = getattr(response, "usage", None) or {}
            thinking_text = getattr(response, "thinking", "") or ""
        prompt_text = _text_of(messages)
        total_len = len(prompt_text) + len(reply)
        tokens = max(1, total_len // 4)
        in_tokens = tokens * len(prompt_text) // total_len if total_len else 0
        out_tokens = tokens - in_tokens
        with self._lock:
            self._record_usage(phase, messages, reply, usage, thinking_text)
            self.calls += 1
            self.tokens_estimated += tokens
            self.in_tokens_estimated += in_tokens
            self.out_tokens_estimated += out_tokens
            self.calls_by_phase[phase] = self.calls_by_phase.get(phase, 0) + 1
            self.tokens_by_phase[phase] = self.tokens_by_phase.get(phase, 0) + tokens
        return reply

    def chat_phase(self, phase: str, messages, sampling=None) -> str:
        return self.chat(messages, phase=phase, sampling=sampling)

    def chat_stream(self, messages, sampling=None, phase: str = "stream"):
        chunks = []
        try:
            stream = self.inner.chat_stream(messages, sampling=sampling)
        except TypeError:
            stream = self.inner.chat_stream(messages)
        for chunk in stream:
            text = str(chunk)
            chunks.append(text)
            yield text
        reply = "".join(chunks)
        prompt_text = _text_of(messages)
        total_len = len(prompt_text) + len(reply)
        tokens = max(1, total_len // 4)
        in_tokens = tokens * len(prompt_text) // total_len if total_len else 0
        out_tokens = tokens - in_tokens
        with self._lock:
            self.calls += 1
            self.tokens_estimated += tokens
            self.in_tokens_estimated += in_tokens
            self.out_tokens_estimated += out_tokens
            self.calls_by_phase["stream"] = self.calls_by_phase.get("stream", 0) + 1
            self.tokens_by_phase["stream"] = self.tokens_by_phase.get("stream", 0) + tokens
        # Streamed deltas carry text only, so this delta is always
        # the split estimate, thinking tokens 0.
        with self._lock:
            self._record_usage(phase, messages, reply, None)

    def report(self) -> dict:
        return {"model_calls": self.calls,
                "model_tokens_estimated": self.tokens_estimated,
                "model_tokens_in_actual": self.in_tokens_actual,
                "model_tokens_out_actual": self.out_tokens_actual,
                "calls_with_usage": self.calls_with_usage,
                "model_tokens_basis":
                    "actual" if self.calls and
                    self.calls_with_usage == self.calls else "estimated",
                "calls_by_phase": dict(self.calls_by_phase),
                "tokens_by_phase": dict(self.tokens_by_phase)}
