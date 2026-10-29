"""Second scheduled-runs batch: expiry, timezone, time-box,
failure-notify wiring pieces, and shell approval.

The daemon tick itself is not run here; these tests cover the
pieces it assembles: SchedulerJob expiry/timezone validation,
the loop deadline machinery, and the scheduled gate's shell rule.
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner, Plan, Step
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.triggers import Scheduler, SchedulerJob
from src.system.main import _goal_extra_tools, _scheduled_gate
from src.system.model_client import FakeClient


def _job(**kwargs):
    kwargs.setdefault("name", "j")
    kwargs.setdefault("schedule", "daily 08:00")
    kwargs.setdefault("goal", "do it")
    return SchedulerJob(**kwargs)


def test_until_caps_next_due():
    job = _job(until="2099-01-01T00:00:00")
    assert job.next_due(datetime(2026, 10, 28, 9, 0)) is not None
    expired = _job(until="2020-01-01T00:00:00")
    assert expired.next_due(datetime(2026, 10, 28, 9, 0)) is None
    # Candidate past until does not fire, even if the schedule does.
    assert _job(schedule="daily 08:00",
                until="2026-10-28T07:00:00").next_due(
                    datetime(2026, 10, 28, 9, 0)) is None


def test_add_job_validates_until_tz_time_limit(tmp_path):
    scheduler = Scheduler(tmp_path / "home")
    with pytest.raises(ValueError, match="not an ISO datetime"):
        scheduler.add_job(_job(until="tomorrow"))
    with pytest.raises(ValueError, match="unknown timezone"):
        scheduler.add_job(_job(tz="Mars/Olympus"))
    with pytest.raises(ValueError, match="time_limit"):
        scheduler.add_job(_job(time_limit=-5))
    scheduler.add_job(_job(time_limit="300"))
    assert scheduler.jobs[0].time_limit == 300


def test_tz_job_returns_zoned_candidate_and_fires_via_check_due(tmp_path):
    job = _job(tz="America/Vancouver")
    nxt = job.next_due(datetime(2026, 10, 26, 14, 0))
    assert nxt is not None and nxt.tzinfo is not None
    assert str(nxt.tzinfo) == "America/Vancouver"
    assert (nxt.hour, nxt.minute) == (8, 0)
    scheduler = Scheduler(tmp_path / "home")
    scheduler.add_job(_job(tz="America/Vancouver"))
    due = scheduler.check_due(datetime(2026, 10, 28, 12, 0))
    assert [j.name for j in due] == ["j"]


def _partial_runner(tmp_path):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = FakeClient("{}")
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
    from src.capabilities.tools import ToolRegistry
    runner.tools = ToolRegistry(runner.sandbox.workdir, sandbox=runner.sandbox)
    runner.allowed_side_effect_tools = frozenset()
    runner.gate_context = None
    runner.network_available = None
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner.model_verifier = None
    return runner


def test_deadline_exceeded_helper():
    runner = GoalRunner.__new__(GoalRunner)
    assert runner._deadline_exceeded() is False
    runner._deadline_at = time.monotonic() - 1
    assert runner._deadline_exceeded() is True
    runner._deadline_at = time.monotonic() + 3600
    assert runner._deadline_exceeded() is False


def test_execute_plan_partial_when_deadline_passed(tmp_path):
    runner = _partial_runner(tmp_path)
    runner._timeboxed = False
    runner._deadline_at = time.monotonic() - 1
    plan = Plan(steps=[
        Step(tool="write", args={"file": "a.md", "text": "x\n"},
             deliverable="a.md"),
        Step(tool="write", args={"file": "b.md", "text": "y\n"},
             deliverable="b.md")],
        deliverables=["a.md", "b.md"], criteria=[])
    report = runner._execute_plan("write two files", plan)
    assert report.status == "partial"
    assert report.files == []
    assert "time limit" in (report.error or "")


def test_execute_plan_runs_when_deadline_open(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    runner = _partial_runner(tmp_path)
    runner._timeboxed = False
    runner._deadline_at = time.monotonic() + 3600
    plan = Plan(steps=[
        Step(tool="write", args={"file": "a.md", "text": "x\n"},
             deliverable="a.md")],
        deliverables=["a.md"], criteria=[])
    report = runner._execute_plan("write one file", plan)
    assert report.status == "done"
    assert report.files == ["a.md"]


def test_partial_is_not_a_failed_example(tmp_path):
    from src.agentic.loop import RunReport
    runner = _partial_runner(tmp_path)

    class _Mem:
        def __init__(self):
            self.failures = []

        def record_failure(self, *args):
            self.failures.append(args)

    mem = _Mem()
    runner.memory = mem
    plan = Plan(steps=[], deliverables=[], criteria=[])
    report = RunReport("partial", "g", plan, False, False, files=["a.md"])
    runner._finalize(report)
    assert mem.failures == []
    assert report.status == "partial"


def test_scheduled_gate_shell_rule():
    open_gate = _scheduled_gate(sandboxed=True)
    closed_gate = _scheduled_gate(sandboxed=False)
    body = "Run shell command: rm -rf build?"
    assert open_gate.ask("mandatory", "Mandatory approval: shell (x)",
                         body).verdict == "approved"
    assert closed_gate.ask("mandatory", "Mandatory approval: shell (x)",
                           body).verdict == "rejected"
    assert open_gate.ask(
        "mandatory", "Mandatory approval: shell (x)",
        "Run shell command: sudo rm -rf build?").verdict == "rejected"
    assert open_gate.ask(
        "mandatory", "Mandatory approval: shell (x)",
        "Run shell command: rm -rf /tmp/build?").verdict == "rejected"


def test_schedule_job_tool_carries_until_tz_time_limit(tmp_path):
    config = SimpleNamespace(home=tmp_path, owner_email="owner@example.com")
    tools = _goal_extra_tools(config, tmp_path)
    result = tools["schedule_job"]("rss", "goal", "0 * * * *", "fetch",
                                  until="2099-01-01T00:00:00",
                                  tz="America/Vancouver", time_limit=300)
    assert "until 2099" in result and "America/Vancouver" in result
    assert "time limit 300s" in result
    jobs = Scheduler(tmp_path).jobs
    assert len(jobs) == 1
    assert jobs[0].until == "2099-01-01T00:00:00"
    assert jobs[0].tz == "America/Vancouver"
    assert jobs[0].time_limit == 300
