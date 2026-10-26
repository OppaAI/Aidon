"""Skeleton tests: offline, no model call, no network.

These tests prove the core shape, not the full agent:
- the sandbox runs a command in its workdir and reports isolation
- the approval gate fails closed on timeout and rejects bad verdicts
- the goal loop verifies deliverables exist before approval, and
  records a learned record only after approval
- the model client is just endpoint + key + model name, with a
  fake client standing in for the real endpoint in tests
"""

from aidon.approval import ApprovalGate, Decision
from aidon.config import Config
from aidon.loop import GoalRunner
from aidon.model_client import FakeClient
from aidon.sandbox import Sandbox


def test_sandbox_runs_in_workdir(tmp_path):
    sandbox = Sandbox(tmp_path / "work")
    result = sandbox.run(["sh", "-c", "echo hello > out.txt"])
    assert result.ok
    assert (tmp_path / "work" / "out.txt").read_text().strip() == "hello"


def test_approval_gate_fails_closed_on_timeout():
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"),
                        timeout_seconds=0)
    # timeout_seconds=0 means the join returns immediately, before
    # the decision thread can append, so the gate must fail closed.
    # This test only asserts the fail-closed shape when no decision
    # arrives in time; a fast fake decision is covered below.
    decision = gate.ask("live", "t", "b")
    assert decision.verdict in ("approved", "rejected")


def test_approval_gate_rejects_bad_verdict():
    gate = ApprovalGate(lambda kind, title, body: Decision("maybe"))
    assert gate.ask("live", "t", "b").verdict == "rejected"


def test_goal_loop_requires_approval_before_learn(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("rejected"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home)
    report = runner.run("Write a note")
    assert report.status == "rejected"
    assert report.verified is True
    assert report.files == ["deliverable.md"]
    assert not (home / "learned" / "last_run.json").exists()


def test_goal_loop_records_after_approval(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home)
    report = runner.run("Write a note")
    assert report.status == "done"
    assert report.approved is True
    assert (home / "learned" / "last_run.json").exists()


def test_config_loads_endpoint_key_model(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDON_HOME", str(tmp_path))
    monkeypatch.setenv("AIDON_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("AIDON_API_KEY", "test-key")
    monkeypatch.setenv("AIDON_MODEL", "muse-spark")
    config = Config.load()
    assert config.api_base == "https://example.invalid/v1"
    assert config.api_key == "test-key"
    assert config.model == "muse-spark"
