"""Shell command normalization: string commands must reach the backend as lists."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from src.agentic.loop import GoalRunner, _normalize_shell_args
from src.capabilities.execution import ExecutionResult
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


def test_normalize_shell_args_table():
    assert _normalize_shell_args({"command": "echo hi"}) == {
        "command": ["sh", "-c", "echo hi"]}
    assert _normalize_shell_args({"command": ["echo", "hi"]}) == {"command": ["echo", "hi"]}
    assert _normalize_shell_args({"cmd": "ls -la"}) == {
        "cmd": "ls -la", "command": ["sh", "-c", "ls -la"]}
    assert _normalize_shell_args({"command": ""}) == {"command": []}
    assert _normalize_shell_args({"command": "echo 'a b'"}) == {
        "command": ["sh", "-c", "echo 'a b'"]}
    assert _normalize_shell_args("not-a-dict") == "not-a-dict"


class _FakeBackend:
    name = "fake"

    def __init__(self):
        self.seen = None

    def run(self, command, workspace, timeout_seconds=None):
        self.seen = command
        return ExecutionResult(0, "ok:" + " ".join(command), "", False)


def _runner(tmp_path, backend):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(FakeClient(""), sandbox, gate, home, execution_backend=backend)


def test_call_tool_shell_string_command_reaches_backend_as_list(tmp_path):
    backend = _FakeBackend()
    runner = _runner(tmp_path, backend)
    assert runner._call_tool("shell", {"command": "echo hi"}) == (
        "ok:sh -c echo hi")
    assert backend.seen == ["sh", "-c", "echo hi"]


class _ExecutingBackend:
    name = "fake"

    def run(self, command, workspace, timeout_seconds=None):
        proc = subprocess.run(command, cwd=workspace, capture_output=True,
                              text=True)
        return ExecutionResult(proc.returncode, proc.stdout, proc.stderr,
                               False)


def test_shell_string_redirection_creates_file(tmp_path):
    backend = _ExecutingBackend()
    runner = _runner(tmp_path, backend)
    result = runner._call_tool(
        "shell",
        {"command": "printf 'transfer-ok' > transfer-check.txt"})
    assert result == ""
    assert (runner.sandbox.workdir / "transfer-check.txt").read_text() == (
        "transfer-ok")


def test_call_tool_shell_empty_command_still_rejected(tmp_path):
    backend = _FakeBackend()
    runner = _runner(tmp_path, backend)
    with pytest.raises(ValueError, match="non-empty list"):
        runner._call_tool("shell", {"command": ""})
