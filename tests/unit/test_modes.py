"""Ask/Plan/Execute modes: parsing, flags, stop store, loop stops, resume."""

import json
from types import SimpleNamespace

import pytest

from src.interface.approval import ApprovalGate, Decision
from src.agentic.interview import Spec
from src.agentic.loop import GoalRunner, Plan, Step
from src.agentic.semantic_router import SemanticRouter
from src.capabilities.sandbox import Sandbox
from src.interface.mode_stops import (
    delete_mode_stop,
    latest_mode_stop,
    load_mode_stop,
    save_mode_stop,
)
from src.interface.surfaces import (
    mode_enabled,
    parse_mode_command,
    parse_proceed_command,
    set_mode,
)
from src.system.model_client import FakeClient


# --- command parsing ---

def test_parse_mode_once_and_set():
    assert parse_mode_command("/ask plan my dinner") == {
        "action": "once", "mode": "ask", "goal": "plan my dinner"}
    assert parse_mode_command("/plan") == {"action": "set", "mode": "plan"}
    assert parse_mode_command("/execute") == {"action": "set", "mode": "execute"}
    assert parse_mode_command("/ASK do it")["mode"] == "ask"


def test_parse_mode_rejects_non_commands():
    assert parse_mode_command("ask me questions") is None
    assert parse_mode_command("/askfoo bar") is None
    assert parse_mode_command("/think hard") is None


def test_parse_proceed():
    assert parse_proceed_command("proceed") == {"stop_id": None}
    assert parse_proceed_command("/proceed abc123") == {"stop_id": "abc123"}
    assert parse_proceed_command("proceed with the plan") is None
    assert parse_proceed_command("please proceed") is None


# --- sticky mode flags ---

def test_mode_flags_round_trip_and_clear(tmp_path):
    assert mode_enabled(tmp_path, "conv-1") == ""
    assert set_mode(tmp_path, "conv-1", "ask") == "ask"
    assert mode_enabled(tmp_path, "conv-1") == "ask"
    assert set_mode(tmp_path, "conv-1", "execute") == ""
    assert mode_enabled(tmp_path, "conv-1") == ""


def test_mode_flags_preserve_other_sections(tmp_path):
    flags_file = tmp_path / "think_flags.json"
    flags_file.write_text(json.dumps({"think": {"conv-1": True}}))
    set_mode(tmp_path, "conv-1", "plan")
    data = json.loads(flags_file.read_text())
    assert data["think"] == {"conv-1": True}
    assert data["modes"] == {"conv-1": "plan"}


# --- stop store ---

def test_mode_stops_save_load_delete_latest(tmp_path):
    first = save_mode_stop(tmp_path, "ask", "plan dinner",
                           spec={"goal": "plan dinner"},
                           conversation_key="conv-1")
    second = save_mode_stop(tmp_path, "plan", "plan dinner",
                            plan={"steps": []}, conversation_key="conv-1")
    save_mode_stop(tmp_path, "ask", "other goal",
                   spec={"goal": "other goal"}, conversation_key="conv-2")
    assert load_mode_stop(tmp_path, first)["mode"] == "ask"
    assert load_mode_stop(tmp_path, "missing") is None
    assert latest_mode_stop(tmp_path, "conv-1")["stop_id"] == second
    assert latest_mode_stop(tmp_path, "conv-2")["goal"] == "other goal"
    assert latest_mode_stop(tmp_path, "nobody") is None
    assert delete_mode_stop(tmp_path, first) is True
    assert load_mode_stop(tmp_path, first) is None
    assert delete_mode_stop(tmp_path, first) is False


def test_mode_stops_reject_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        save_mode_stop(tmp_path, "execute", "do it")


# --- router mode detection (fake embedder, fixed vectors) ---

class _FakeEmbedder:
    def embed_batch(self, texts):
        out = []
        for text in texts:
            lowered = text.lower()
            if "questions first" in lowered:
                out.append([1.0, 0.0, 0.0])
            elif "plan only" in lowered:
                out.append([0.0, 1.0, 0.0])
            elif "both modes" in lowered:
                out.append([0.707, 0.707, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


def _router_with_modes():
    return SemanticRouter(_FakeEmbedder(), example_vectors={
        "chat": [[0.0, 0.0, 1.0]],
        "webchat": [[0.0, 0.0, 1.0]],
        "agentic": [[0.0, 0.0, 1.0]],
        "artifact": [[0.0, 0.0, 1.0]],
        "web_intent": [],
        "modes": {"ask": [[1.0, 0.0, 0.0]], "plan": [[0.0, 1.0, 0.0]]},
    })


def test_router_mode_for():
    router = _router_with_modes()
    assert router.mode_for("ask me questions first") == "ask"
    assert router.mode_for("plan only, don't run it") == "plan"
    assert router.mode_for("both modes at once") is None
    assert router.mode_for("what's the weather?") is None


# --- loop: Ask stop ---

def _ask_runner(tmp_path):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = FakeClient("")
    runner.router = None
    runner.stream_callback = None
    runner.think_max = False
    runner.research_mode = False
    runner._effort_escalated = False
    runner._creative_mode = False
    runner._deep_research_context = ""
    runner.kernel_mode = False
    runner.plugins = None
    runner.extra_tools = {}
    runner.guidance = ""
    runner.memory = None
    runner.trace = None
    runner._trace_started = {}
    runner.run_control = None
    runner.budget = None
    runner.image_paths = []
    runner.sandbox = Sandbox(tmp_path / "work", 120)
    runner.home = tmp_path
    runner.run_id = "test-run"
    runner.project_id = "default"
    runner.execution_backend = None
    runner.gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner.allowed_side_effect_tools = frozenset()
    runner.gate_context = None
    runner.semantic_router = None
    runner.network_available = None
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner.mode = "ask"
    runner.question_ask = lambda q: "everything"
    runner.conversation_key = "conv-1"
    runner.resume_plan = None
    return runner


def test_ask_mode_stops_with_spec(tmp_path, monkeypatch):
    class _StubInterview:
        def __init__(self, *args, **kwargs):
            pass

        def start_from_message(self, message, ask, approve=None):
            assert ask("what should it cover?") == "everything"
            assert approve is None
            return SimpleNamespace(
                spec=Spec(goal=message, deliverables=["notes.md"]))

    monkeypatch.setattr(
        "src.agentic.interview.InterviewRunner", _StubInterview)
    runner = _ask_runner(tmp_path)
    report = runner.run("plan my dinner")
    assert report.status == "stopped"
    assert report.mode_stop["mode"] == "ask"
    stop = load_mode_stop(tmp_path, report.mode_stop["stop_id"])
    assert stop is not None
    assert stop["spec"]["deliverables"] == ["notes.md"]
    assert stop["conversation_key"] == "conv-1"
    assert not (tmp_path / "work" / "notes.md").exists()


def test_ask_mode_without_question_channel_fails(tmp_path):
    runner = _ask_runner(tmp_path)
    runner.question_ask = None
    report = runner.run("plan my dinner")
    assert report.status == "failed"
    assert "question channel" in report.error


# --- loop: Plan stop + resume ---

def _plan_runner(tmp_path, mode):
    runner = _ask_runner(tmp_path)
    runner.mode = mode
    return runner


def test_plan_mode_stops_before_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    runner = _plan_runner(tmp_path, "plan")
    plan = Plan(steps=[Step(tool="write", args={"file": "a.md", "text": "x\n"},
                            deliverable="a.md")],
                deliverables=["a.md"], criteria=[])
    report = runner._execute_plan("make notes", plan)
    assert report.status == "stopped"
    assert report.mode_stop["mode"] == "plan"
    assert report.mode_stop["deliverables"] == ["a.md"]
    assert not (tmp_path / "work" / "a.md").exists()
    stop = load_mode_stop(tmp_path, report.mode_stop["stop_id"])
    assert stop["plan"]["deliverables"] == ["a.md"]


def test_proceed_plan_executes_saved_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    plan = Plan(steps=[Step(tool="write", args={"file": "a.md", "text": "x\n"},
                            deliverable="a.md")],
                deliverables=["a.md"], criteria=[])
    runner = GoalRunner(
        FakeClient(""), Sandbox(tmp_path / "work", 120),
        ApprovalGate(lambda kind, title, body: Decision("approved")),
        tmp_path, memory=None, resume_plan=plan, mode="execute")
    report = runner.run("make notes")
    assert report.status == "done"
    assert (tmp_path / "work" / "a.md").read_text() == "x\n"


def test_proceed_ask_runs_with_frozen_spec(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    plan_json = ('{"steps": [{"tool": "write", "args": {"file": "a.md", '
                 '"text": "x\\n"}, "deliverable": "a.md"}], '
                 '"deliverables": ["a.md"], "criteria": []}')
    runner = GoalRunner(
        FakeClient(plan_json), Sandbox(tmp_path / "work", 120),
        ApprovalGate(lambda kind, title, body: Decision("approved")),
        tmp_path, memory=None, mode="execute")
    report = runner.run(
        "make notes", spec=Spec(goal="make notes", deliverables=["a.md"]))
    assert report.status == "done"
    assert (tmp_path / "work" / "a.md").read_text() == "x\n"


# --- WebUI /proceed dispatch ---

def test_webui_proceed_dispatches_resume(tmp_path):
    import time

    from src.interface.triggers import TriggerDispatcher
    from src.interface.webui import WebUIServer

    home = tmp_path / "home"
    home.mkdir()
    stop_id = save_mode_stop(
        home, "plan", "make notes",
        plan={"steps": [{"tool": "write", "args": {"file": "a.md", "text": "x\n"},
                         "deliverable": "a.md"}],
              "deliverables": ["a.md"], "criteria": []},
        conversation_key="c1")
    events = []
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", events.append)
    server = WebUIServer(home, dispatcher=dispatcher, memory=None)
    result = server.start_goal({"goal": f"/proceed {stop_id}",
                                "conversation_id": "c1"})
    assert "run_id" in result
    deadline = time.time() + 5
    while not events and time.time() < deadline:
        time.sleep(0.05)
    assert len(events) == 1
    event = events[0]
    assert event.goal == "make notes"
    assert event.payload["mode"] == "execute"
    assert event.payload["resume_stop_id"] == stop_id
    assert event.payload["resume_plan"]["deliverables"] == ["a.md"]
    # Bare proceed with no stop is a text reply, not a dispatched run.
    server.start_goal({"goal": "/proceed", "conversation_id": "other"})
    time.sleep(0.2)
    assert len(events) == 1
