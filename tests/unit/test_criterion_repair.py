"""Criterion repair: unverifiable-only failures rewrite the
assertions and re-verify the untouched files, instead of
re-executing the plan and minting another attempt's files.
"""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


def _bare_runner(workdir, chat_reply):
    runner = GoalRunner.__new__(GoalRunner)
    runner._chat = lambda phase, messages: chat_reply
    runner.sandbox = SimpleNamespace(workdir=workdir)
    return runner


def test_repair_criteria_returns_replacement(tmp_path):
    runner = _bare_runner(
        tmp_path,
        '{"criteria": ["report.pdf exists in the workspace folder"]}')
    out = runner._repair_criteria(
        "Write a report", ["do a good job"],
        ["unverified criterion: do a good job"])
    assert out == ["report.pdf exists in the workspace folder"]


def test_repair_criteria_rejects_garbage_and_errors(tmp_path):
    runner = _bare_runner(tmp_path, "not json at all")
    assert runner._repair_criteria("g", ["do a good job"],
                                  ["unverified criterion: do a good job"]) is None
    runner._chat = lambda phase, messages: (_ for _ in ()).throw(
        RuntimeError("budget"))
    assert runner._repair_criteria("g", ["do a good job"],
                                  ["unverified criterion: do a good job"]) is None
    runner._chat = lambda phase, messages: '{"criteria": []}'
    assert runner._repair_criteria("g", ["do a good job"],
                                  ["unverified criterion: do a good job"]) is None


def test_repair_saves_run_without_reexecution(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "report.pdf").write_bytes(b"%PDF-")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path)
    calls = []

    def _chat(phase, messages):
        calls.append(phase)
        return json.dumps(
            {"criteria": ["report.pdf exists in the workspace folder"]})

    runner._chat = _chat
    from src.agentic.loop import Step
    plan = Plan(steps=[Step(tool="write",
                           args={"file": "report.pdf", "text": "body"},
                           deliverable="report.pdf")],
                deliverables=["report.pdf"],
                criteria=["do a good job"])
    report = runner._execute_plan("Write a report", plan)
    assert report.status == "done"
    assert report.attempts == 1
    assert "report.pdf" in report.files
    # Exactly one plan-phase model call: the criterion repair.
    # (critic and approval phrasing use other phases.) A replan
    # would have added a second plan call, and a re-execution
    # would have minted a numbered duplicate.
    assert calls.count("plan") == 1
    assert not list(work.glob("report_*.pdf"))


def test_spent_criterion_repair_fails_fast(tmp_path):
    # Once criterion repair has been spent and verification still
    # fails only on uncheckable criteria, the run must fail fast
    # with the criteria named, not burn the remaining attempts
    # re-executing a plan whose assertions can never pass. The
    # old code re-planned and re-executed to attempt 3.
    work = tmp_path / "work"
    work.mkdir(parents=True)
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path)

    def _chat(phase, messages):
        if phase == "plan":
            # Criterion repair, returning soft criteria again.
            return json.dumps({"criteria": ["do an even better job"]})
        # Repair plan, valid steps but the same soft criterion.
        return json.dumps({
            "steps": [{"tool": "write",
                       "args": {"file": "report.md", "text": "body"},
                       "deliverable": "report.md"}],
            "deliverables": ["report.md"],
            "criteria": ["do a good job"],
        })

    runner._chat = _chat
    from src.agentic.loop import Step
    plan = Plan(steps=[Step(tool="write",
                           args={"file": "report.md", "text": "body"},
                           deliverable="report.md")],
                deliverables=["report.md"],
                criteria=["do a good job"])
    report = runner._execute_plan("Write a report", plan)
    assert report.status == "failed"
    assert report.attempts == 1
    assert "criteria not checkable" in (report.error or "")
    assert "do a good job" in (report.error or "")
