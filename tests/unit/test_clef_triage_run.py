"""Triage in run(): blocked stops before planning, need_x plans
with the gap note, and a failed triage never blocks a run."""

from types import SimpleNamespace

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


class _StubClef:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence

    def decide_choice(self, state, name, instructions, criteria):
        return self.choice, self.confidence, {}

    def ensure_warmed(self):
        return None


def _runner(work, tmp_path, clef):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path,
                      clef_client=clef)


def test_blocked_triage_stops_before_planning(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path, _StubClef("blocked", 0.95))
    calls = []
    runner._chat = lambda phase, messages: calls.append(phase) or "hi"
    report = runner.run("Connect to a system Aidon cannot reach")
    assert report.status == "failed"
    assert "stopping before planning" in (report.error or "")
    assert calls == [], "no model call may run for a blocked goal"


def test_need_triage_plans_with_note_and_completes(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path, _StubClef("need_mcp", 0.9))
    traces = []
    runner._trace = lambda kind, status, detail="": traces.append(
        (kind, status, detail))
    report = runner.run("Write hello.txt with hello")
    assert report.status == "done"
    assert any(kind == "capability" and "need_mcp" in detail
               for kind, _status, detail in traces)


def test_failed_triage_never_blocks(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)

    class _Broken:
        def decide_choice(self, *args, **kwargs):
            raise RuntimeError("clef down")

    runner = _runner(work, tmp_path, _Broken())
    traces = []
    runner._trace = lambda kind, status, detail="": traces.append(
        (kind, status, detail))
    report = runner.run("Write hello.txt with hello")
    assert report.status == "done"
    assert any(kind == "capability" and status == "triage-skipped"
               for kind, status, _detail in traces)
