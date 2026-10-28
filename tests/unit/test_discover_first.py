"""Discover-before-building planner rule, and the kernel-mode PDF guard.

In normal mode the planner is told about generate_document, so a
model that writes its own fpdf script is bypassing a listed tool.
In kernel mode that tool is deliberately unavailable, so the guard
must point at the plugin ladder, not at a tool that is not there.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


def _runner(tmp_path, kernel_mode, extra_tools=None):
    def decide(kind, title, body):
        return Decision("approved")

    return GoalRunner(FakeClient("hi"), Sandbox(tmp_path / "work"),
                      ApprovalGate(decide), tmp_path,
                      kernel_mode=kernel_mode,
                      extra_tools=extra_tools or {})


def test_plan_prompt_carries_discover_first(tmp_path):
    captured = {}

    def fake_chat(phase, messages):
        captured["system"] = messages[0].content
        return json.dumps({
            "steps": [{"tool": "write",
                       "args": {"name": "x.txt", "text": "hi"},
                       "deliverable": "x.txt"}],
            "deliverables": ["x.txt"],
            "criteria": ["x exists"],
        })

    runner = _runner(tmp_path, False,
                     extra_tools={"generate_document": lambda **kw: "ok"})
    runner._chat = fake_chat
    runner._plan("Write x.")
    assert "Discover before building" in captured["system"]
    assert "If a listed tool does the job, use it" in captured["system"]
    assert "depends on a package you have not confirmed" in captured["system"]


def test_kernel_guard_points_at_ladder_not_missing_tool(tmp_path):
    runner = _runner(tmp_path, True,
                     extra_tools={"generate_document": lambda **kw: "ok"})
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "make_pdf.py").write_text("import fpdf\n")
    with pytest.raises(ValueError) as excinfo:
        runner._call_tool("shell", {"command": ["python3", "make_pdf.py"]})
    message = str(excinfo.value)
    assert "plugin ladder" in message
    assert "not available here" in message
    assert "available generate_document tool instead" not in message
    with pytest.raises(ValueError) as excinfo2:
        runner._call_tool("shell",
                          {"command": ["python3", "-c", "import fpdf"]})
    assert "plugin ladder" in str(excinfo2.value)


def test_nonkernel_guard_message_unchanged(tmp_path):
    runner = _runner(tmp_path, False,
                     extra_tools={"generate_document": lambda **kw: "ok"})
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (work / "make_pdf.py").write_text("import fpdf\n")
    with pytest.raises(ValueError) as excinfo:
        runner._call_tool("shell", {"command": ["python3", "make_pdf.py"]})
    assert "available generate_document tool instead" in str(excinfo.value)
