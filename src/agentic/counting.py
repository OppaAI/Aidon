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

Per-step display (the right-bar trace) reads the same data
through take_phase_usage(): real usage when the backend
reported it — prompt_tokens in, completion_tokens out,
completion_tokens_details.reasoning_tokens as thinking
tokens — with a chars//4 split estimate when it did not,
exactly like old Aidon's rule. take_phase_usage drains the
phase's delta without touching the cumulative totals, so the
finished-message total never changes when a trace event
reads its step's tokens. Streamed calls have no usage to
read (server-sent deltas carry text only), so they stay
estimated, thinking tokens 0.
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
        self._thinking_text: dict[str, str] = {}
        # Parallel LLM steps count through here concurrently;
        # without a lock, increments from two threads can
        # interleave and silently lose counts.
        self._lock = threading.Lock()

    def _split_estimate(self, messages, reply: str) -> tuple[int, int]:
        """Chars//4 split estimate, old Aidon's fallback rule."""
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
        else:
            tokens_in, tokens_out = self._split_estimate(messages, reply)
            thinking = 0
        self._usage_in[phase] = self._usage_in.get(phase, 0) + tokens_in
        self._usage_out[phase] = self._usage_out.get(phase, 0) + tokens_out
        self._usage_thinking[phase] = \
            self._usage_thinking.get(phase, 0) + thinking
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
        tokens = max(1, (len(_text_of(messages)) + len(reply)) // 4)
        with self._lock:
            self._record_usage(phase, messages, reply, usage, thinking_text)
            self.calls += 1
            self.tokens_estimated += tokens
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
        tokens = max(1, (len(_text_of(messages)) + len(reply)) // 4)
        with self._lock:
            self.calls += 1
            self.tokens_estimated += tokens
            self.calls_by_phase["stream"] = self.calls_by_phase.get("stream", 0) + 1
            self.tokens_by_phase["stream"] = self.tokens_by_phase.get("stream", 0) + tokens
        # Streamed deltas carry text only, so this delta is always
        # the split estimate, thinking tokens 0.
        with self._lock:
            self._record_usage(phase, messages, reply, None)

    def report(self) -> dict:
        return {"model_calls": self.calls,
                "model_tokens_estimated": self.tokens_estimated,
                "calls_by_phase": dict(self.calls_by_phase),
                "tokens_by_phase": dict(self.tokens_by_phase)}
