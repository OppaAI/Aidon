"""Plan validation for extra tools: an extra tool step must pass
validation and reach _call_tool dispatch, not fail the run up front."""

import json

from src.agentic.loop import GoalRunner, Plan
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision


def _runner(tmp_path, extra_tools=None):
    return GoalRunner(FakeClient(""),
                      Sandbox(tmp_path / "work"),
                      ApprovalGate(lambda kind, title, body: Decision("approved", "test")),
                      tmp_path, extra_tools=extra_tools)


def _plan_with_extra_step():
    return Plan(
        steps=[{"tool": "write", "args": {"file": "job.txt", "text": "job"},
                "deliverable": "job.txt"},
               {"tool": "schedule_job",
                "args": {"name": "daily", "schedule": "0 9 * * *", "goal": "report"},
                "deliverable": ""}],
        deliverables=["job.txt"], criteria=[])


def test_extra_tool_step_passes_validation_and_executes(tmp_path):
    calls = []

    def schedule_job(**kw):
        calls.append(kw)
        return "scheduled"

    runner = _runner(tmp_path, extra_tools={"schedule_job": schedule_job})
    report = runner._execute_plan("schedule a job", _plan_with_extra_step())
    assert calls == [{"name": "daily", "schedule": "0 9 * * *", "goal": "report"}]
    assert report.status != "failed", report.error
    assert "not allowed" not in (report.error or "")


def test_unknown_tool_never_executes(tmp_path):
    # Dev's bounded repair pass replaces an invalid plan instead of
    # failing up front. The invariant that survives: the unknown
    # tool never executes and never survives into the final plan.
    runner = _runner(tmp_path, extra_tools={"schedule_job": lambda **kw: "scheduled"})
    plan = Plan(
        steps=[{"tool": "bogus_tool_xyz", "args": {}, "deliverable": "x.md"}],
        deliverables=["x.md"], criteria=[])
    report = runner._execute_plan("do the bogus thing", plan)
    tools = [s.get("tool", "") if isinstance(s, dict) else getattr(s, "tool", "")
             for s in report.plan.steps]
    assert "bogus_tool_xyz" not in tools


def test_schedule_job_without_extra_tools_never_executes(tmp_path):
    # Same repair contract: without the extra tool registered, the
    # schedule_job step is repaired away, never executed.
    runner = _runner(tmp_path, extra_tools=None)
    report = runner._execute_plan("schedule a job", _plan_with_extra_step())
    tools = [s.get("tool", "") if isinstance(s, dict) else getattr(s, "tool", "")
             for s in report.plan.steps]
    assert "schedule_job" not in tools


def test_plan_prompt_lists_extra_tools_when_set(tmp_path):
    captured = {}

    def fake_chat(phase, messages):
        captured["system"] = messages[0].content if messages else ""
        return json.dumps({"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"},
                                      "deliverable": "a.md"}],
                           "deliverables": ["a.md"], "criteria": []})

    runner = _runner(tmp_path, extra_tools={"schedule_job": lambda **kw: "ok",
                                            "make_spreadsheet": lambda **kw: "ok"})
    runner._chat = fake_chat
    runner._plan("schedule a job")
    assert "schedule_job" in captured["system"]
    assert "make_spreadsheet" in captured["system"]


def test_plan_prompt_unchanged_without_extra_tools(tmp_path):
    captured = {}

    def fake_chat(phase, messages):
        captured["system"] = messages[0].content if messages else ""
        return json.dumps({"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"},
                                      "deliverable": "a.md"}],
                           "deliverables": ["a.md"], "criteria": []})

    runner = _runner(tmp_path, extra_tools=None)
    runner._chat = fake_chat
    runner._plan("schedule a job")
    assert "Extra tools" not in captured["system"]
    assert "schedule_job" not in captured["system"]


def test_cycle_check_uses_widened_set(tmp_path):
    # Two extra-tool steps depending on each other: validation must
    # pass (both tools are allowed), and the cycle check must still
    # catch the cycle rather than the run proceeding or failing on
    # tool names.
    runner = _runner(tmp_path, extra_tools={"schedule_job": lambda **kw: "ok"})
    plan = Plan(
        steps=[{"tool": "schedule_job", "args": {}, "deliverable": "a.md",
                "depends_on": ["step-1"]},
               {"tool": "schedule_job", "args": {}, "deliverable": "b.md",
                "depends_on": ["step-0"]}],
        deliverables=["a.md", "b.md"], criteria=[])
    report = runner._execute_plan("cyclic extra tools", plan)
    assert report.status == "failed"
    assert "cycle" in (report.error or "").lower()
