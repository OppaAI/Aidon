"""#4 fixes: file renames, fpdf-script guard, waiting questions.

Covers the agentic PDF failure loop: a FILE_EXISTS string used to be
marked as a passed step, then repair skipped that deliverable forever
while nothing was written. Renames break that loop, the shell guard
closes the write-a-script-then-run-it bypass around the fpdf ban,
and ask_user finally waits for a real answer on the WebUI.
"""

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.webui import WebUIServer
from src.system.main import _goal_extra_tools
from src.system.model_client import FakeClient


class _Config:
    home = None
    github_token = ""
    threads_account_id = ""
    threads_access_token = ""
    ig_user_id = ""
    ig_access_token = ""
    fish_api_key = ""
    tts_voice_id = ""
    transcribe_api_key = ""
    owner_email = "owner@example.com"


def _tools(tmp_path, **kwargs):
    cfg = _Config()
    cfg.home = tmp_path
    return _goal_extra_tools(cfg, tmp_path / "work", **kwargs)


def test_write_file_renames_instead_of_file_exists(tmp_path):
    tools = _tools(tmp_path)
    tools["write_file"](name="note.txt", text="original")
    second = tools["write_file"](name="note.txt", text="second")
    assert (tmp_path / "work" / "note.txt").read_text() == "original"
    assert "renamed" in str(second)
    assert Path(second.path).name == "note_2.txt"
    assert Path(second.path).read_text() == "second"


def test_generate_document_renames_and_writes_real_pdf(tmp_path):
    tools = _tools(tmp_path)
    sections = [{"heading": "About", "body": "Line one.\nLine two."}]
    tools["generate_document"](title="Me", sections=sections,
                               filename="me.pdf")
    second = tools["generate_document"](title="Me", sections=sections,
                                        filename="me.pdf")
    assert (tmp_path / "work" / "me.pdf").read_bytes().startswith(b"%PDF")
    assert "renamed" in str(second)
    assert Path(second.path).name == "me_2.pdf"
    assert Path(second.path).read_bytes().startswith(b"%PDF")


def test_ask_user_uses_question_channel_when_present(tmp_path):
    tools = _tools(tmp_path, question_answer=lambda q: "the answer")
    assert tools["ask_user"]("what should I write?") == "the answer"


def test_ask_user_without_channel_says_so(tmp_path):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    tools = _tools(tmp_path, gate=gate)
    result = tools["ask_user"]("what should I write?")
    assert "NOT_DELIVERED" in result
    assert result != "what should I write?"


def _runner(tmp_path):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient("hi"), Sandbox(tmp_path / "work"),
                        gate, tmp_path, extra_tools={"generate_document": lambda **kw: "doc"})
    runner.tools = SimpleNamespace(call=lambda name, args: "ran")
    return runner


def test_shell_guard_refuses_fpdf_script_by_content(tmp_path):
    runner = _runner(tmp_path)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "make_pdf.py").write_text("from fpdf import FPDF\n")
    with pytest.raises(ValueError, match="generate_document"):
        runner._call_tool("shell", {"command": ["python3", "make_pdf.py"]})


def test_shell_guard_allows_clean_script(tmp_path):
    runner = _runner(tmp_path)
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "clean.py").write_text("print('hello')\n")
    assert runner._call_tool("shell", {"command": ["python3", "clean.py"]}) == "ran"


def test_question_waiter_blocks_until_answered(tmp_path):
    server = WebUIServer(tmp_path)
    server._web_runs["run-1"] = {"run_id": "run-1", "events": [], "trace": []}
    answers = []

    def asker():
        answers.append(server.ask_question("run-1", "conv-1", "What to write?"))

    thread = threading.Thread(target=asker, daemon=True)
    thread.start()
    import time
    for _ in range(100):
        if server.waiting_question("run-1") == "What to write?":
            break
        time.sleep(0.05)
    assert server.waiting_question("run-1") == "What to write?"
    assert server.answer_question("conv-1", "about me, in detail") == "run-1"
    thread.join(timeout=5)
    assert answers == ["about me, in detail"]
    assert server.waiting_question("run-1") is None
    assert server.answer_question("conv-1", "late") is None
