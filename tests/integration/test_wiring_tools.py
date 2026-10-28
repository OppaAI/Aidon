"""Wiring: extra tools into goal runs, and model-router phase dispatch.

No Gmail MCP here: that direction is dropped by the owner, Gmail
API send already exists via the email draft/sender path.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.main import _goal_extra_tools
from src.system.model_client import FakeClient
from src.system.model_router import RouterClient


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


def _tools(tmp_path, gate=None):
    cfg = _Config()
    cfg.home = tmp_path
    return _goal_extra_tools(cfg, tmp_path / "work", gate=gate)


def test_extra_tools_contains_expected_names(tmp_path):
    tools = _tools(tmp_path)
    for name in ("make_bar_chart", "make_line_chart", "generate_document",
                 "build_ebook", "github_search", "github_fetch",
                 "threads_publish", "instagram_publish", "tts_speak",
                 "transcribe_audio", "email_make_draft", "email_send",
                 "ask_user", "declare_blocked", "schedule_job"):
        assert name in tools, f"missing extra tool {name}"
    # Office builders are guarded: if office.py is absent they
    # are skipped, never faked; in this repo it exists, so they
    # are wired, confined to the workdir like the charts.
    assert "make_spreadsheet" in tools
    assert "make_docx" in tools
    assert "make_slides" in tools
    assert "make_latex" in tools


def test_extra_tools_chart_writes_under_workdir(tmp_path):
    tools = _tools(tmp_path)
    out = tools["make_bar_chart"]("Sales", [("a", 10), ("b", 20)], "chart.svg")
    assert out.endswith("chart.svg")
    assert (tmp_path / "work" / "chart.svg").is_file()
    assert "20" in (tmp_path / "work" / "chart.svg").read_text()


def test_extra_tools_gated_names_go_through_gate(tmp_path):
    asks = []

    def decide(kind, title, body):
        asks.append((kind, title))
        return Decision("approved")

    gate = ApprovalGate(decide)
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    tools = _tools(tmp_path, gate=gate)
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, extra_tools=tools)
    # threads_publish needs account/token, so it fails inside the
    # tool, but the loop gate must still have been asked first.
    runner._call_tool("threads_publish", {"text": "hello"})
    assert asks, "loop gate was not asked for a publish tool"
    assert any("threads_publish" in title for _kind, title in asks)
    # email_send is likewise gated by name, before dispatch.
    asks.clear()
    draft = tools["email_make_draft"]("someone@example.com", "Hi", "body")
    runner._call_tool("email_send", {"draft": draft})
    assert asks, "loop gate was not asked for a send tool"
    # A rejected gate fails closed, before the tool runs.
    gate2 = ApprovalGate(lambda kind, title, body: Decision("rejected"))
    runner2 = GoalRunner(FakeClient(""), sandbox, gate2, home, extra_tools=tools)
    try:
        runner2._call_tool("threads_publish", {"text": "hello"})
        raise AssertionError("rejected publish should have raised")
    except ValueError as exc:
        assert "rejected by approval gate" in str(exc)


def test_ask_user_declare_blocked_schedule_job(tmp_path):
    tools = _tools(tmp_path, gate=None)
    assert tools["ask_user"]("what is the budget?") == "no gate configured"
    blocked = tools["declare_blocked"]("waiting on owner")
    assert "blocked" in blocked and "waiting on owner" in blocked
    result = tools["schedule_job"]("daily-report", "report", "0 9 * * *", "write the daily report")
    assert "daily-report" in result and "0 9 * * *" in result
    from src.interface.triggers import Scheduler
    scheduler = Scheduler(tmp_path)
    assert any(job.name == "daily-report" for job in scheduler.jobs)


def test_router_phase_dispatch(tmp_path):
    fast = FakeClient("fast-reply")
    main = FakeClient("main-reply")
    router = RouterClient({"interpret": fast, "learn": fast, "respond": fast},
                          default=main)
    assert router.chat_phase("respond", []) == "fast-reply"
    assert router.chat_phase("plan", []) == "main-reply"
    assert router.chat("interpret", []) == "fast-reply"
    assert router.chat([]) == "main-reply"


def test_goal_runner_router_uses_fast_for_respond(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    fast = FakeClient("fast answer")
    main = FakeClient("main answer")
    router = RouterClient({"respond": fast}, default=main)

    class ChatRouter:
        def classify(self, goal):
            return ("chat", 0.9)

    runner = GoalRunner(main, sandbox, gate, home, router=router,
                        semantic_router=ChatRouter(),
                        use_semantic_router_skip=True)
    report = runner.run("What is 2 + 2?")
    assert report.status == "chat"
    assert report.chat_reply == "fast answer"
    assert len(fast.calls) == 1
    assert len(main.calls) == 0


def test_goal_runner_without_router_behaves_as_before(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))

    class ChatRouter:
        def classify(self, goal):
            return ("chat", 0.9)

    runner = GoalRunner(FakeClient("plain answer"), sandbox, gate, home,
                        semantic_router=ChatRouter(),
                        use_semantic_router_skip=True)
    report = runner.run("What is 2 + 2?")
    assert report.status == "chat"
    assert report.chat_reply == "plain answer"
