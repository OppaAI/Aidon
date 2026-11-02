"""Per-step trace tokens: real usage when the backend reports it,
chars//4 split estimate when it does not, drained per trace event."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.counting import CountingClient
from src.agentic.loop import GoalRunner
from src.system.model_client import ChatResponse, FakeClient, Message


class UsageFake(FakeClient):
    def chat_response(self, messages, sampling=None):
        return ChatResponse(
            text="reply",
            usage={"prompt_tokens": 100, "completion_tokens": 50,
                   "completion_tokens_details": {"reasoning_tokens": 30}},
            thinking="some reasoning")


def _msg(text="hello"):
    return [Message(role="user", content=text)]


def test_usage_recorded_and_drained_per_phase():
    client = CountingClient(UsageFake())
    assert client.chat(_msg(), phase="interpret") == "reply"
    usage = client.take_phase_usage("interpret")
    assert usage == {"tokens_in": 100, "tokens_out": 50,
                     "thinking_tokens": 30, "thinking": "some reasoning"}
    # Drained: the next take is zeros, but cumulative totals stand.
    assert client.take_phase_usage("interpret")["tokens_in"] == 0
    assert client.calls == 1
    assert client.tokens_estimated == max(1, (5 + 5) // 4)


def test_estimate_fallback_without_usage():
    client = CountingClient(FakeClient("ok"))
    assert client.chat(_msg("hello world"), phase="plan") == "ok"
    usage = client.take_phase_usage("plan")
    assert usage["tokens_in"] == max(1, len("hello world") // 4)
    assert usage["tokens_out"] == max(1, len("ok") // 4)
    assert usage["thinking_tokens"] == 0
    assert usage["thinking"] == ""


def test_stream_delta_lands_under_its_phase():
    class StreamFake(FakeClient):
        def chat_stream(self, messages, sampling=None):
            yield "a"
            yield "b"
    client = CountingClient(StreamFake())
    assert "".join(client.chat_stream(_msg("hi"), phase="respond")) == "ab"
    usage = client.take_phase_usage("respond")
    assert usage["tokens_in"] == max(1, len("hi") // 4)
    assert usage["tokens_out"] == max(1, len("ab") // 4)
    assert usage["thinking_tokens"] == 0


class _StubTrace:
    def record(self, kind, status, detail, run_id=None):
        return None


def _runner_with(client):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = client
    runner.router = None
    runner.trace = _StubTrace()
    runner.run_control = None
    runner.run_id = None
    seen = []
    runner.trace_callback = seen.append
    runner._trace_started = {}
    return runner, seen


def test_trace_done_event_carries_step_tokens():
    counting = CountingClient(UsageFake())
    runner, seen = _runner_with(counting)
    runner._trace("interpret", "started", "interpreting")
    assert seen[-1]["tokens_in"] == 0
    assert seen[-1]["seconds"] == 0
    counting.chat(_msg(), phase="interpret")
    runner._trace("interpret", "done", "routed")
    payload = seen[-1]
    assert payload["tokens_in"] == 100
    assert payload["tokens_out"] == 50
    assert payload["thinking_tokens"] == 30
    assert "thinking" not in payload
    assert payload["seconds"] >= 0


def test_trace_without_counting_client_stays_zero():
    runner, seen = _runner_with(FakeClient("x"))
    runner._trace("sandbox", "done", "write")
    payload = seen[-1]
    assert payload["tokens_in"] == 0
    assert payload["tokens_out"] == 0
    assert payload["thinking_tokens"] == 0
    assert "thinking" not in payload
