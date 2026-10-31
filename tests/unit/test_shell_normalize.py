"""Shell command normalization: string commands must reach the backend as lists."""

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
    assert _normalize_shell_args({"command": "echo hi"}) == {"command": ["echo", "hi"]}
    assert _normalize_shell_args({"command": ["echo", "hi"]}) == {"command": ["echo", "hi"]}
    assert _normalize_shell_args({"cmd": "ls -la"}) == {"cmd": "ls -la", "command": ["ls", "-la"]}
    assert _normalize_shell_args({"command": ""}) == {"command": []}
    assert _normalize_shell_args({"command": "echo 'a b'"}) == {"command": ["echo", "a b"]}
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
    assert runner._call_tool("shell", {"command": "echo hi"}) == "ok:echo hi"
    assert backend.seen == ["echo", "hi"]


def test_call_tool_shell_empty_command_still_rejected(tmp_path):
    backend = _FakeBackend()
    runner = _runner(tmp_path, backend)
    with pytest.raises(ValueError, match="non-empty list"):
        runner._call_tool("shell", {"command": ""})
