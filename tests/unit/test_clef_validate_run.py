"""Validate gate inside _execute_plan: accept skips the model
verifier, high-confidence retry repairs, failure defers."""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan, Step
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


class _StubClef:
    def __init__(self, actions):
        # actions: list of (choice, confidence), consumed in order.
        self.actions = list(actions)
        self.calls = 0

    def decide_choice(self, state, name, instructions, criteria):
        self.calls += 1
        choice, confidence = self.actions[min(self.calls - 1,
                                              len(self.actions) - 1)]
        return choice, confidence, {}


def _runner(work, tmp_path, clef, verifier):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path,
                        clef_client=clef)
    runner.model_verifier = verifier
    return runner


def _plan():
    return Plan(
        steps=[Step(tool="write", args={"file": "out.json", "text": "v1"},
                    deliverable="out.json")],
        deliverables=["out.json"],
        criteria=["out.json exists"])


def _verifier_stub(calls):
    def _verify(goal, deliverables, workdir, criteria):
        calls.append(goal)
        return {"verdict": "yes", "reason": "YES fine"}
    return SimpleNamespace(verify=_verify, effort_escalated=False)


def test_accept_skips_model_verifier(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", "always")
    work = tmp_path / "work"
    work.mkdir(parents=True)
    calls = []
    runner = _runner(work, tmp_path, _StubClef([("accept", 0.9)]),
                     _verifier_stub(calls))
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert calls == [], "gate accept must skip the model verifier"


def test_retry_repairs_then_accept_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", "always")
    work = tmp_path / "work"
    work.mkdir(parents=True)
    calls = []
    runner = _runner(work, tmp_path,
                     _StubClef([("retry", 0.9), ("accept", 0.9)]),
                     _verifier_stub(calls))
    repair_json = json.dumps({
        "steps": [{"tool": "write", "args": {"file": "out.json", "text": "v2"},
                   "deliverable": "out.json"}],
        "deliverables": ["out.json"],
        "criteria": ["out.json exists"]})
    runner._chat = lambda phase, messages: repair_json
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert (work / "out.json").read_text() == "v2"
    assert calls == [], "gate retry must not need the model verifier"


def test_gate_failure_defers_to_model_verifier(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", "always")
    work = tmp_path / "work"
    work.mkdir(parents=True)
    calls = []

    class _Broken:
        def decide_choice(self, *args, **kwargs):
            raise RuntimeError("clef down")

    runner = _runner(work, tmp_path, _Broken(), _verifier_stub(calls))
    report = runner._execute_plan("Write out.json", _plan())
    assert report.status == "done"
    assert len(calls) == 1, "a failed gate must defer to the model verifier"
