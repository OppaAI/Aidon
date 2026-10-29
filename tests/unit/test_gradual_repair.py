"""Gradual repair: a model-verifier NO re-runs only the named files.

Code failures already repaired gradually (passed steps skipped). A
model NO used to wipe every passed file (passed.clear()), forcing a
full regeneration that could break good files. Now only deliverables
named in the NO reason leave the passed set; the rest are kept.
"""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan, Step
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


def _repair_plan_json():
    return json.dumps({
        "steps": [
            {"tool": "write", "args": {"file": "a.txt", "text": "v2"},
             "deliverable": "a.txt"},
            {"tool": "write", "args": {"file": "b.txt", "text": "CHANGED"},
             "deliverable": "b.txt"},
        ],
        "deliverables": ["a.txt", "b.txt"],
        "criteria": ["a.txt exists", "b.txt exists"],
    })


def _runner(work, tmp_path):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path)


def test_model_no_repairs_only_named_file(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", "always")
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path)
    calls = {"n": 0}

    def _verify(goal, deliverables, workdir, criteria):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"verdict": "no", "reason": "NO: a.txt is thin"}
        return {"verdict": "yes", "reason": "YES fine"}

    runner.model_verifier = SimpleNamespace(verify=_verify, effort_escalated=False)
    runner._chat = lambda phase, messages: _repair_plan_json()
    plan = Plan(
        steps=[
            Step(tool="write", args={"file": "a.txt", "text": "v1"},
                 deliverable="a.txt"),
            Step(tool="write", args={"file": "b.txt", "text": "v1"},
                 deliverable="b.txt"),
        ],
        deliverables=["a.txt", "b.txt"],
        criteria=["a.txt exists", "b.txt exists"])
    report = runner._execute_plan("Write two files", plan)
    assert report.status == "done"
    assert report.attempts == 2
    assert (work / "a.txt").read_text(encoding="utf-8") == "v2"
    # b.txt kept its first-attempt content: its step was skipped.
    assert (work / "b.txt").read_text(encoding="utf-8") == "v1"


def test_model_verifier_off_skips_the_call(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_MODEL_VERIFIER", raising=False)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path)
    calls = {"n": 0}

    def _verify(goal, deliverables, workdir, criteria):
        calls["n"] += 1
        return {"verdict": "no", "reason": "NO: everything"}

    runner.model_verifier = SimpleNamespace(verify=_verify, effort_escalated=False)
    runner._chat = lambda phase, messages: _repair_plan_json()
    plan = Plan(
        steps=[Step(tool="write", args={"file": "a.txt", "text": "v1"},
                    deliverable="a.txt")],
        deliverables=["a.txt"], criteria=["a.txt exists"])
    report = runner._execute_plan("Write one file", plan)
    assert report.status == "done"
    assert report.attempts == 1
    assert calls["n"] == 0
