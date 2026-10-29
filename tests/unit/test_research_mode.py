"""Research mode: /research toggle + one-shot on every surface,
research_mode implies think-max, deep findings ride into plans."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner
from src.interface.surfaces import (
    CLI_HELP_TEXT, HELP_TEXT, load_research_flags, load_think_flags,
    parse_research_command, research_enabled, set_research, set_think,
    toggle_research)
from src.system.model_client import FakeClient, Message


def test_parse_research_command():
    assert parse_research_command("/research") == {"action": "toggle"}
    assert parse_research_command("/research find prices") == {
        "action": "once", "goal": "find prices"}
    assert parse_research_command("/RESEARCH  spaced  ") == {
        "action": "once", "goal": "spaced"}
    assert parse_research_command("/researcher x") is None
    assert parse_research_command("research this") is None


def test_research_flags_share_file_with_think(tmp_path):
    set_think(tmp_path, "c1", True)
    set_research(tmp_path, "c1", True)
    assert research_enabled(tmp_path, "c1") is True
    # A think toggle must not wipe the research section.
    from src.interface.surfaces import toggle_think
    toggle_think(tmp_path, "c1")
    assert load_think_flags(tmp_path).get("c1") is None
    assert research_enabled(tmp_path, "c1") is True
    assert toggle_research(tmp_path, "c1") is False
    assert load_research_flags(tmp_path).get("c1") is None


def test_help_lists_research():
    assert "/research" in HELP_TEXT
    assert "/research <goal>" in HELP_TEXT
    assert "/research" in CLI_HELP_TEXT
    assert "/research <goal>" in CLI_HELP_TEXT


def _runner_with(client, research_mode=False):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = client
    runner.router = None
    runner.stream_callback = None
    runner.think_max = False
    runner.research_mode = research_mode
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
    return runner


def test_research_mode_implies_think_max():
    client = FakeClient("ok")
    runner = _runner_with(client, research_mode=True)
    assert runner._chat("plan", [Message(role="user", content="hi")]) == "ok"
    assert client.last_sampling["reasoning_effort"] == "max"


def test_research_mode_off_keeps_normal_sampling():
    client = FakeClient("ok")
    runner = _runner_with(client, research_mode=False)
    runner._chat("plan", [Message(role="user", content="hi")])
    assert client.last_sampling.get("reasoning_effort") is None


def test_plan_carries_deep_research_context():
    plan_json = json.dumps({
        "steps": [{"tool": "write", "args": {"file": "a.md", "text": "x\n"},
                   "deliverable": "a.md"}],
        "deliverables": ["a.md"], "criteria": []})
    client = FakeClient(plan_json)
    runner = _runner_with(client, research_mode=True)
    runner._deep_research_context = "DEEP FINDINGS: roast beef $12"
    plan = runner._plan("write a report")
    assert plan.deliverables == ["a.md"]
    sent = " ".join(str(m.content) for call in client.calls for m in call)
    assert "DEEP FINDINGS: roast beef $12" in sent


def test_make_deep_researcher_wires_config_and_chat(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_DEEP_RESEARCH_ROUNDS", raising=False)
    monkeypatch.delenv("AIDON_DEEP_RESEARCH_SOURCES", raising=False)
    import src.capabilities.documents as _docs
    import src.capabilities.search as _search_mod
    from src.system.main import make_deep_researcher

    monkeypatch.setattr(_search_mod, "search", lambda *a, **k: {
        "results": [{"title": "A", "url": "https://a.example/x",
                     "snippet": "roast beef guide"}]})
    monkeypatch.setattr(_docs, "read_document",
                        lambda url: "roast beef\n\ncook it slowly")
    config = SimpleNamespace(search_backend="searxng", tavily_api_key="",
                             searxng_url="http://127.0.0.1:9", home=tmp_path,
                             research_allowlist="")
    researcher = make_deep_researcher(config)

    def chat(messages):
        if "full page fetch" in messages[0].content:
            return '{"fetch": [0]}'
        return "Summary with citation [https://a.example/x]."
    report = researcher("roast beef prices", chat)
    assert report["citations"] == ["https://a.example/x"]
    assert "https://a.example/x" in report["summary"]
