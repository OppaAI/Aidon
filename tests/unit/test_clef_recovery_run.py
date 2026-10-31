"""Recovery router inside _execute_plan: retry skips the
replan, escalate stops the run, acquire steers the repair,
errors and low confidence repair as today."""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan, Step
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


class _RouteClef:
    def __init__(self, actions, error=None):
        self.actions = list(actions)
        self.error = error
        self.route_calls = 0

    def decide_choice(self, state, name, instructions, criteria):
        if name != "recovery_route":
            return "accept", 0.9, {}
        if self.error is not None:
            raise self.error
        self.route_calls += 1
        route, confidence = self.actions[min(self.route_calls - 1,
                                             len(self.actions) - 1)]
        return route, confidence, {}


def _runner(work, tmp_path, clef):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path,
                        clef_client=clef)
    runner.model_verifier = None
    return runner


def _plan():
    return Plan(
        steps=[Step(tool="write", args={"file": "out.json", "text": "v1"},
                    deliverable="out.json")],
        deliverables=["out.json"],
        criteria=["out.json contains hello"])


def _fixed_plan_json():
    return json.dumps({
        "steps": [{"tool": "write",
                   "args": {"file": "out.json", "text": "hello"},
                   "deliverable": "out.json"}],
        "deliverables": ["out.json"],
        "criteria": ["out.json contains hello"]})


def test_retry_skips_replan_then_repair_finishes(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([("retry_execution", 0.9),
                                 ("repair_artifact", 0.9)]))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert (work / "out.json").read_text() == "hello"
    assert report.attempts == 3, \
        "retry consumes an attempt without replanning"
    assert len(chats) == 1, \
        "retry must not ask for a repaired plan; only the repair does"


def test_retry_only_once_then_repairs(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([("retry_execution", 0.9),
                                 ("retry_execution", 0.9)]))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert report.attempts == 3, \
        "first retry consumes an attempt; second becomes a repair"
    assert len(chats) == 1, "the second retry must become a repair"


def test_high_confidence_escalate_stops_run(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([("escalate", 0.9)]))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "failed"
    assert report.attempts == 1
    assert "escalated" in (report.error or "")
    assert chats == [], "escalate must not attempt a repair"


def test_low_confidence_escalate_repairs_as_today(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([("escalate", 0.5)]))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert len(chats) == 1


def test_acquire_capability_steers_repair_prompt(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([("acquire_capability", 0.9)]))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    blob = json.dumps(chats, default=str)
    assert "capability ladder" in blob


def test_router_error_repairs_as_today(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    chats = []
    runner = _runner(work, tmp_path,
                     _RouteClef([], error=RuntimeError("clef down")))
    runner._chat = lambda phase, messages: (
        chats.append(messages), _fixed_plan_json())[1]
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert len(chats) == 1
