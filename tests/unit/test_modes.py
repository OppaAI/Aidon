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
    enrich_mode_stop,
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


def test_mode_stops_update_spec_and_latest_for_conversation(tmp_path):
    from src.interface.mode_stops import (
        latest_mode_stop_for_conversation, mode_stops_for_conversation,
        update_mode_stop_spec)
    stop_id = save_mode_stop(tmp_path, "ask", "plan dinner",
                             spec={"goal": "plan dinner"},
                             conversation_key="conv-1",
                             conversation_id="conv-1")
    assert update_mode_stop_spec(tmp_path, stop_id,
                                 {"goal": "plan dinner",
                                  "deliverables": ["menu.pdf"]}) is True
    assert load_mode_stop(tmp_path, stop_id)["spec"]["deliverables"] == ["menu.pdf"]
    assert update_mode_stop_spec(tmp_path, "missing", {}) is False
    # Key and id both match: a WebUI stop is findable either way.
    assert latest_mode_stop_for_conversation(tmp_path, "conv-1")["stop_id"] == stop_id
    assert latest_mode_stop_for_conversation(tmp_path, "nobody") is None
    assert latest_mode_stop_for_conversation(tmp_path, "") is None


def test_mode_stop_keeps_originating_slack_thread(tmp_path):
    from src.interface.mode_stops import mode_stops_for_conversation
    stop_id = save_mode_stop(
        tmp_path, "plan", "make a PDF", plan={"steps": []},
        conversation_key="slack:D1", conversation_id="slack:D1",
        thread_ts="1700000000.123456")
    stops = mode_stops_for_conversation(tmp_path, "slack:D1")
    assert [stop["stop_id"] for stop in stops] == [stop_id]
    assert stops[0]["thread_ts"] == "1700000000.123456"
    assert mode_stops_for_conversation(tmp_path, "slack:D2") == []


def test_mode_stop_spec_save_round_trip(tmp_path):
    from src.interface.webui import WebUIServer
    from src.interface.mode_stops import load_mode_stop, save_mode_stop
    server = WebUIServer(tmp_path, memory=None)
    stop_id = save_mode_stop(tmp_path, "ask", "plan dinner",
                             spec={"goal": "plan dinner"},
                             conversation_key="conv-1",
                             conversation_id="conv-1")
    result = server.handle_mode_stop_spec_save(stop_id, {"spec": {
        "goal": "plan dinner", "criteria": ["menu.pdf exists"],
        "deliverables": ["menu.pdf"], "budget": "$25.00"}})
    assert result["ok"] is True
    assert result["verified"] is True
    assert result["spec"]["criteria"] == ["menu.pdf exists"]
    assert result["spec"]["budget_cents"] == 2500
    assert load_mode_stop(tmp_path, stop_id)["spec"]["criteria"] == ["menu.pdf exists"]
    saved = tmp_path / "specs" / "conv-1.json"
    assert saved.exists()
    # Unverified still saves, with failures reported, not hidden.
    bad = server.handle_mode_stop_spec_save(stop_id, {"spec": {
        "goal": "plan dinner", "deliverables": ["menu.pdf"],
        "open_questions": ["which cuisine?"]}})
    assert bad["ok"] is True
    assert bad["verified"] is False
    assert bad["failures"]
    # Plan stops carry no spec; missing stops 404-style.
    plan_id = save_mode_stop(tmp_path, "plan", "plan dinner",
                             plan={"steps": []}, conversation_key="conv-plan")
    assert server.handle_mode_stop_spec_save(plan_id, {"spec": {}})["ok"] is False
    assert server.handle_mode_stop_spec_save("missing", {"spec": {}})["ok"] is False
    latest = server.handle_mode_stop_latest("conv-1")
    assert latest is not None and latest["spec"]["goal"] == "plan dinner"
    assert server.handle_mode_stop_latest("nobody") is None


def test_proceed_ask_stop_goes_to_plan_mode(tmp_path):
    # Mode chain: Ask -> Plan -> Execute. An Ask stop's Proceed
    # builds a plan for approval; a Plan stop's Proceed executes.
    import time
    from src.interface.webui import WebUIServer
    from src.interface.mode_stops import save_mode_stop
    seen = []

    class _Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def dispatch(self, event):
            seen.append(event)

    server = WebUIServer(tmp_path, dispatcher=_Dispatcher(), memory=None)
    ask_id = save_mode_stop(tmp_path, "ask", "plan dinner",
                            spec={"goal": "plan dinner"},
                            conversation_key="conv-1",
                            conversation_id="conv-1")
    plan_id = save_mode_stop(tmp_path, "plan", "plan dinner",
                             plan={"steps": []}, conversation_key="conv-1",
                             conversation_id="conv-1")
    result = server.start_goal({"goal": f"/proceed {ask_id}",
                                "conversation_id": "conv-1"})
    assert "run_id" in result
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen[0].payload["mode"] == "plan"
    assert seen[0].payload["resume_spec"] == {"goal": "plan dinner"}
    assert seen[0].payload["resume_stop_id"] == ask_id
    seen.clear()
    result = server.start_goal({"goal": f"/proceed {plan_id}",
                                "conversation_id": "conv-1"})
    assert "run_id" in result
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen[0].payload["mode"] == "execute"
    assert seen[0].payload["resume_plan"] == {"steps": []}


def test_mode_stop_delete_via_server(tmp_path):
    from src.interface.webui import WebUIServer
    from src.interface.mode_stops import load_mode_stop, save_mode_stop
    server = WebUIServer(tmp_path, memory=None)
    stop_id = save_mode_stop(tmp_path, "ask", "plan dinner",
                             spec={"goal": "plan dinner"},
                             conversation_key="conv-1")
    assert server.handle_mode_stop_delete(stop_id) == {"ok": True}
    assert load_mode_stop(tmp_path, stop_id) is None
    assert server.handle_mode_stop_delete(stop_id) == {"ok": False}
    assert server.handle_mode_stop_latest("conv-1") is None


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
    runner.conversation_id = "test"
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
    runner.conversation_thread_id = "1700000000.123456"
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
    assert stop["thread_ts"] == "1700000000.123456"


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


def test_explicit_execute_prevents_semantic_plan_stop(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")

    class _PlanDetectingRouter:
        mode_calls = 0

        def classify(self, text, judge=None):
            return ("agentic", 1.0)

        def mode_for(self, text):
            self.mode_calls += 1
            return "plan"

    router = _PlanDetectingRouter()
    plan_json = ('{"steps": [{"tool": "write", "args": '
                 '{"file": "scheduler-smoke.txt", '
                 '"text": "scheduler-ok\\n"}, '
                 '"deliverable": "scheduler-smoke.txt"}], '
                 '"deliverables": ["scheduler-smoke.txt"], '
                 '"criteria": ["scheduler-smoke.txt exists"]}')
    runner = GoalRunner(
        FakeClient(plan_json), Sandbox(tmp_path / "work", 120),
        ApprovalGate(lambda kind, title, body: Decision("approved")),
        tmp_path, semantic_router=router, mode="execute")

    report = runner.run("Create scheduler-smoke.txt in the workspace")

    assert router.mode_calls == 0
    assert report.status == "done"
    assert (tmp_path / "work" / "scheduler-smoke.txt").read_text() == "scheduler-ok\n"


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


def test_enrich_mode_stop_attaches_spec_and_plan(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    spec = {"goal": "make tarts", "deliverables": ["12 tarts"],
            "constraints": ["milk-free"], "assumptions": [],
            "open_questions": []}
    ask_id = save_mode_stop(home, "ask", "make tarts", spec=spec,
                            conversation_key="c1")
    enriched = enrich_mode_stop(home, {"stop_id": ask_id, "mode": "ask",
                                       "goal": "make tarts"})
    assert enriched["spec"] == spec
    assert "plan" not in enriched
    plan = {"steps": [{"tool": "write", "args": {}, "deliverable": "a.md"}],
            "deliverables": ["a.md"], "criteria": []}
    plan_id = save_mode_stop(home, "plan", "make notes", plan=plan,
                             conversation_key="c1")
    enriched_plan = enrich_mode_stop(home, {"stop_id": plan_id, "mode": "plan",
                                            "goal": "make notes"})
    assert enriched_plan["plan"] == plan
    assert enrich_mode_stop(home, {"stop_id": "missing", "mode": "ask",
                                   "goal": "x"}) == {"stop_id": "missing",
                                                    "mode": "ask", "goal": "x"}
