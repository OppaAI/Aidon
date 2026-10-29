"""Tuning flags: repair attempts, replans, plan repairs, ReAct rounds/workers.

Defaults preserve the old hardcoded behavior (3 attempts, 1 replan,
2 plan repairs, 8 ReAct rounds); the flags only make them tunable.
Verify has no flag by design: the verifier runs once per attempt,
so verify passes scale with attempts.
"""

import json
import threading

import pytest

from src.agentic.loop import GoalRunner, Plan, Step, _resolve_count, run_react
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import ChatResponse, ToolCall

FAIL_PLAN = {"steps": [{"tool": "shell", "args": {"command": ["false"]},
                        "deliverable": "out.txt"}],
             "deliverables": ["out.txt"], "criteria": []}
GOOD_PLAN = {"steps": [{"tool": "write", "args": {"file": "ok.md", "text": "ok\n"},
                        "deliverable": "ok.md"}],
             "deliverables": ["ok.md"], "criteria": []}
BAD_PLAN = {"steps": [{"tool": "nope", "args": {}, "deliverable": "x.md"}],
            "deliverables": ["x.md"], "criteria": []}


class _QueueClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def chat(self, messages, sampling=None):
        self.calls += 1
        return self.replies.pop(0)

    def chat_phase(self, phase, messages, sampling=None):
        return self.chat(messages, sampling=sampling)


def _runner(client, tmp_path, researcher=None):
    return GoalRunner(client, Sandbox(tmp_path),
                      ApprovalGate(lambda kind, title, body: Decision("approved", "test")),
                      tmp_path, researcher=researcher)


def test_resolve_count(monkeypatch):
    monkeypatch.delenv("AIDON_MAX_ATTEMPTS", raising=False)
    assert _resolve_count("AIDON_MAX_ATTEMPTS", 3, 1) == 3
    monkeypatch.setenv("AIDON_MAX_ATTEMPTS", "7")
    assert _resolve_count("AIDON_MAX_ATTEMPTS", 3, 1) == 7
    monkeypatch.setenv("AIDON_MAX_ATTEMPTS", "bogus")
    assert _resolve_count("AIDON_MAX_ATTEMPTS", 3, 1) == 3
    monkeypatch.setenv("AIDON_MAX_ATTEMPTS", "0")
    assert _resolve_count("AIDON_MAX_ATTEMPTS", 3, 1) == 1
    assert _resolve_count("AIDON_MAX_ATTEMPTS", 3, 0) == 0


def test_settings_catalog_has_tuning_flags():
    from src.interface.webui import WebUIServer
    entries = {s["key"]: s for group in WebUIServer.SETTINGS_CATALOG
               for s in group["settings"]}
    expected = {"AIDON_MAX_ATTEMPTS": "3", "AIDON_MAX_REPLANS": "1",
                "AIDON_MAX_PLAN_REPAIRS": "2", "AIDON_REACT_MAX_ROUNDS": "8",
                "AIDON_REACT_MAX_WORKERS": "3"}
    for key, default in expected.items():
        assert entries[key]["type"] == "int"
        assert entries[key]["default"] == default


def test_max_attempts_env(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_MAX_ATTEMPTS", raising=False)
    # The critic is a separate axis: it adds one model call per
    # valid plan, which would consume this test's scripted queue.
    # Disable it so the test measures attempts, not critic calls.
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    client = _QueueClient([json.dumps(FAIL_PLAN), json.dumps(GOOD_PLAN)])
    report = _runner(client, tmp_path).run("Do the thing")
    assert report.status == "done" and report.attempts == 2

    monkeypatch.setenv("AIDON_MAX_ATTEMPTS", "1")
    client = _QueueClient([json.dumps(FAIL_PLAN), json.dumps(GOOD_PLAN)])
    report = _runner(client, tmp_path).run("Do the thing")
    assert report.status == "failed" and report.attempts == 1
    assert client.calls == 1  # no repair plan, no approval lead-in


def test_max_replans_env(tmp_path, monkeypatch):
    first = {"steps": [{"tool": "write", "args": {"file": "a.md", "text": "a\n"},
                        "deliverable": "a.md"}],
             "deliverables": ["a.md"], "criteria": [], "needs_research": ["q2"]}
    second = {"steps": [{"tool": "write", "args": {"file": "b.md", "text": "b\n"},
                         "deliverable": "b.md"}],
              "deliverables": ["b.md"], "criteria": []}

    def run_with(limit):
        monkeypatch.setenv("AIDON_MAX_REPLANS", str(limit))
        researched = []
        client = _QueueClient([json.dumps(first), json.dumps(second)])
        runner = _runner(client, tmp_path,
                         researcher=lambda item: researched.append(item) or "note")
        original = Plan(steps=[Step(tool="write", args={"file": "orig.md", "text": "o\n"},
                                    deliverable="orig.md")],
                        deliverables=["orig.md"], needs_research=["q1"])
        return runner._research_and_replan(original, "goal"), researched

    result, researched = run_with(2)
    assert researched == ["q1", "q2"] and result.deliverables == ["b.md"]
    result, researched = run_with(1)
    assert researched == ["q1"] and result.deliverables == ["a.md"]
    result, researched = run_with(0)
    assert researched == [] and result.deliverables == ["orig.md"]


def test_max_plan_repairs_env(tmp_path, monkeypatch):
    # Repair candidates must stay non-empty: an empty plan fails
    # the run immediately, before the repair-try count is reached.
    monkeypatch.delenv("AIDON_MAX_PLAN_REPAIRS", raising=False)
    client = _QueueClient([json.dumps(BAD_PLAN)] * 4)
    report = _runner(client, tmp_path).run("Do the thing")
    assert report.status == "failed" and client.calls == 3  # 1 initial + 2 repairs

    monkeypatch.setenv("AIDON_MAX_PLAN_REPAIRS", "3")
    client = _QueueClient([json.dumps(BAD_PLAN)] * 5)
    report = _runner(client, tmp_path).run("Do the thing")
    assert report.status == "failed" and client.calls == 4  # 1 initial + 3 repairs


def test_react_parallel_within_round_keeps_call_order():
    barrier = threading.Barrier(2)

    def wait_a():
        barrier.wait(timeout=5)
        return "A"

    def wait_b():
        barrier.wait(timeout=5)
        return "B"

    class Client:
        def __init__(self):
            self.responses = [
                ChatResponse(tool_calls=[ToolCall("a", {}), ToolCall("b", {})]),
                ChatResponse(text="done"),
            ]

        def chat_response(self, messages, phase=None):
            return self.responses.pop(0)

    result = run_react(Client(), [], {"a": wait_a, "b": wait_b})
    # The barrier passing is the parallel proof: sequentially, the
    # first tool would time out waiting for the second. (The
    # tool_calls field counts client calls minus the final text
    # call, not tool invocations, so it is not asserted here.)
    assert result["text"] == "done"
    tool_messages = [m.content for m in result["messages"] if m.role == "tool"]
    assert tool_messages == ["A", "B"]


def test_react_sequential_fallback_and_rounds_env(monkeypatch):
    class Client:
        def __init__(self):
            self.responses = [
                ChatResponse(tool_calls=[ToolCall("a", {}), ToolCall("b", {})]),
                ChatResponse(text="done"),
            ]

        def chat_response(self, messages, phase=None):
            return self.responses.pop(0)

    result = run_react(Client(), [], {"a": lambda: "A", "b": lambda: "B"},
                       max_workers=1)
    tool_messages = [m.content for m in result["messages"] if m.role == "tool"]
    assert tool_messages == ["A", "B"]

    class AlwaysToolClient:
        def chat_response(self, messages, phase=None):
            return ChatResponse(tool_calls=[ToolCall("echo", {"text": "x"})])

    monkeypatch.setenv("AIDON_REACT_MAX_ROUNDS", "2")
    result = run_react(AlwaysToolClient(), [], {"echo": lambda text: text})
    assert result["error"] == "ReAct round limit exceeded" and result["rounds"] == 2

    monkeypatch.delenv("AIDON_REACT_MAX_ROUNDS", raising=False)
    result = run_react(AlwaysToolClient(), [], {"echo": lambda text: text})
    assert result["rounds"] == 8


def test_react_workers_env_respected(monkeypatch):
    barrier = threading.Barrier(2)

    def wait_a():
        barrier.wait(timeout=2)
        return "A"

    def wait_b():
        barrier.wait(timeout=2)
        return "B"

    class Client:
        def __init__(self):
            self.responses = [
                ChatResponse(tool_calls=[ToolCall("a", {}), ToolCall("b", {})]),
                ChatResponse(text="done"),
            ]

        def chat_response(self, messages, phase=None):
            return self.responses.pop(0)

    monkeypatch.setenv("AIDON_REACT_MAX_WORKERS", "1")
    with pytest.raises(threading.BrokenBarrierError):
        run_react(Client(), [], {"a": wait_a, "b": wait_b})
