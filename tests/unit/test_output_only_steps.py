"""Result-only tool steps return evidence without making fake files."""

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


def test_output_only_shell_step_runs_and_returns_its_output(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", "off")
    home = tmp_path / "home"
    home.mkdir()
    plan_json = ('{"summary":"Show the sandbox directory", "steps": ['
                 '{"id":"show-directory", "tool":"shell", '
                 '"args":{"command":["pwd"]}, '
                 '"description":"Show the sandbox working directory"}], '
                 '"deliverables":[], "criteria":[]}')
    runner = GoalRunner(
        FakeClient(plan_json),
        Sandbox(home / "work", backend="disabled", require_isolation=False),
        ApprovalGate(lambda *_: Decision("approved")), home,
        mode="execute")
    runner._call_tool = lambda name, args: "/workspace/run-123\n"

    report = runner.run("Run pwd and report the result")

    assert report.status == "done"
    assert report.verified is True
    assert report.files == []
    assert "/workspace/run-123" in report.result_text
    assert not list((home / "work").iterdir())


def test_planner_accepts_null_as_no_file_deliverable(tmp_path):
    payload = ('{"steps": [{"tool": "shell", '
               '"args": {"command": ["pwd"]}, "deliverable": null}], '
               '"deliverables": null, "criteria": []}')
    home = tmp_path / "home"
    home.mkdir()
    runner = GoalRunner(
        FakeClient(payload),
        Sandbox(home / "work", backend="disabled", require_isolation=False),
        ApprovalGate(lambda *_: Decision("approved")), home)

    plan = runner._plan("Run pwd and report its output")

    assert len(plan.steps) == 1
    assert plan.steps[0]["deliverable"] is None
    assert plan.deliverables == []
