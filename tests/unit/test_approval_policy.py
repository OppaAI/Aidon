"""Workspace-folder terminology, Auto-Approve policy table, blocked reporting.

- "Workspace folder" is the user's phrase for the workdir: same
  folder, same checks. The verifier accepts it, and the planner
  prompt defines the equivalence.
- The pre-approval help text publishes the A/B/C policy table;
  Auto-Approve covers B only.
- Rejected approval gates are recorded and rendered as the
  blocked-actions section of the final report.
"""

from src.agentic.loop import GoalRunner, Plan, RunReport
from src.agentic.verify import _relative_name, check_criterion
from src.interface.approval import Decision
from src.interface.phrasing import report_text
from src.interface.preapproval import HELP_TEXT


class _FakeGate:
    def __init__(self, decision):
        self.decision = decision

    def ask(self, kind, title, body, **kwargs):
        return self.decision


def _runner(gate):
    runner = GoalRunner.__new__(GoalRunner)
    runner.gate = gate
    runner.gate_context = None
    runner._elapsed_timer = None
    runner._blocked = []
    return runner


# --- workspace folder = workdir ---

def test_workspace_folder_exists(tmp_path):
    workdir = tmp_path / "2026-10-29_08-00-00_000000"
    workdir.mkdir()
    (workdir / "report.pdf").write_bytes(b"%PDF-")
    ok, msg = check_criterion(workdir, "report.pdf exists in workspace folder")
    assert ok and msg == "report.pdf exists"
    ok, _ = check_criterion(workdir,
                            "report.pdf exists in the workspace folder")
    assert ok
    ok, msg = check_criterion(workdir,
                              "missing.pdf exists in workspace folder")
    assert not ok and msg == "missing.pdf missing"
    ok, _ = check_criterion(
        workdir, "PDF file report.pdf exists in workspace folder")
    assert ok


def test_workspace_folder_inside_pattern(tmp_path):
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "doc.pdf").write_bytes(b"%PDF-")
    ok, _ = check_criterion(workdir,
                            "PDF file created inside workspace folder")
    assert ok


def test_relative_name_strips_workspace_folder(tmp_path):
    workdir = tmp_path / "w"
    assert _relative_name(workdir, "workspace folder/report.pdf") \
        == "report.pdf"
    assert _relative_name(workdir, "workdir/report.pdf") == "report.pdf"


# --- policy table ---

def test_help_text_carries_categories():
    assert "Categories (Auto-Approve covers B only):" in HELP_TEXT
    assert "A — No approval needed" in HELP_TEXT
    assert "B — Auto-approvable when granted" in HELP_TEXT
    assert "C — Always asks a human" in HELP_TEXT
    assert "publish" in HELP_TEXT


# --- blocked reporting ---

def test_ask_records_timeout_and_declined():
    runner = _runner(_FakeGate(Decision("rejected", reason="timeout")))
    runner._ask_for_approval("publish", "Publish report", "body")
    assert runner._blocked == [
        "publish: Publish report (timed out, still awaiting)"]
    runner.gate = _FakeGate(Decision("rejected", reason=""))
    runner._ask_for_approval("send", "Send to X", "body")
    assert runner._blocked[1] == "send: Send to X (declined)"
    runner.gate = _FakeGate(Decision("approved"))
    runner._ask_for_approval("live", "Go live", "body")
    assert len(runner._blocked) == 2


def test_report_text_renders_blocked():
    text = report_text(
        "done", ["a.pdf"], "", "run1", "stats",
        blocked=["publish: Publish report (timed out, still awaiting)"])
    assert "Blocked actions (need a human):" in text
    assert "- publish: Publish report (timed out, still awaiting)" in text
    assert text.endswith("stats")
    assert "Blocked actions" not in report_text(
        "done", [], "", "run1", "stats")


def test_finalize_copies_blocked(tmp_path):
    runner = GoalRunner.__new__(GoalRunner)
    runner.home = tmp_path
    runner.memory = None
    runner.network_available = True
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner._replan_note = ""
    runner._blocked = ["publish: Publish report (timed out, still awaiting)"]
    runner.client = _FakeGate(Decision("approved"))
    runner.router = None
    runner.run_id = ""
    report = RunReport("failed", "goal",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       False, False)
    out = runner._finalize(report)
    assert out.blocked == ["publish: Publish report (timed out, still awaiting)"]
