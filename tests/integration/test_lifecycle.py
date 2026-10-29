"""Integration coverage for lifecycle, execution, research, plugins, and routing."""

"""Plugin adoption binding, size limits, and offline execution hints.

- Adoption is bound to the approved bytes: a tree hash computed
  before the approval card is shown on the card and re-checked
  immediately before the copy; a changed source aborts adoption.
- Adoption rejects a source tree over MAX_PLUGIN_BYTES, before
  any approval card.
- A security gate that throws now rejects adoption instead of
  presenting a broken scanner as a clean scan; the per-file
  OSError continue is unchanged.
- A failed shell tool in a network-less sandbox keeps its
  "shell failed:" prefix and stderr cap but appends an offline
  hint; with network True or unknown the message is unchanged.
"""

import json
from types import SimpleNamespace

import pytest

from src.capabilities import security_gate
from src.capabilities.plugins import (
    MAX_PLUGIN_BYTES,
    PluginRegistry,
    plugin_tree_hash,
    plugin_tree_size,
)
from src.capabilities.tools import ToolRegistry
from src.interface.approval import Decision


def _plugin_dir(tmp_path, name="upper", code=None):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": []}), encoding="utf-8")
    (directory / "tool.py").write_text(
        code or "def upper(args):\n    return str(args.get('text', '')).upper()\n",
        encoding="utf-8")
    return directory


class _CapturingGate:
    """Approval gate stand-in that records the card body."""

    def __init__(self):
        self.bodies = []

    def ask(self, kind, title, body):
        self.bodies.append(body)
        return Decision("approved")


def test_tree_hash_is_deterministic_and_covers_all_files(tmp_path):
    directory = _plugin_dir(tmp_path)
    (directory / "notes.md").write_text("hello\n", encoding="utf-8")
    (directory / "sub").mkdir()
    (directory / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
    first = plugin_tree_hash(directory)
    assert first == plugin_tree_hash(directory)
    (directory / "sub" / "deep.txt").write_text("changed\n", encoding="utf-8")
    assert plugin_tree_hash(directory) != first
    # An added file the copy would copy must change the hash too.
    (directory / "extra.txt").write_text("new\n", encoding="utf-8")
    assert plugin_tree_hash(directory) != first
    assert plugin_tree_size(directory) > 0


def test_card_shows_approved_hash_and_rehash_matches(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    gate = _CapturingGate()
    ok, _, _ = registry.adopt(directory, gate=gate)
    assert ok is True
    assert len(gate.bodies) == 1
    assert plugin_tree_hash(directory) in gate.bodies[0]
    assert "hash" in gate.bodies[0].lower()


def test_source_changed_during_approval_aborts_and_keeps_old_install(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    # First adopt a known-good version.
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True)
    assert ok is True
    installed = (home / "plugins" / "upper" / "tool.py").read_text(encoding="utf-8")

    def approve_and_mutate(plugin, findings):
        # The owner approves, but the source changes before the copy.
        (directory / "tool.py").write_text(
            "def upper(args):\n    return 'MUTATED'\n", encoding="utf-8")
        return True

    ok, _, message = registry.adopt(directory, approve=approve_and_mutate)
    assert ok is False
    assert "changed after approval" in message
    assert "hash" in message.lower()
    # The abort happens before any rmtree: the old install survives.
    assert (home / "plugins" / "upper" / "tool.py").read_text(encoding="utf-8") == installed
    assert registry.call("upper", {"text": "hi"}) != "MUTATED"


def test_size_cap_rejects_before_approval_card(tmp_path):
    assert MAX_PLUGIN_BYTES == 10 * 1024 * 1024
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    (directory / "blob.bin").write_bytes(b"x" * (MAX_PLUGIN_BYTES + 1))
    called = []
    ok, _, message = registry.adopt(
        directory, approve=lambda plugin, f: called.append(True) or True)
    assert ok is False
    assert "over" in message and "cap" in message
    assert called == [], "oversized source must not reach an approval card"
    assert not (home / "plugins" / "upper").exists()


def test_gate_throwing_rejects_instead_of_clean_scan(tmp_path, monkeypatch):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)

    def broken_gate(source, text, client=None):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(security_gate, "gate_text", broken_gate)
    called = []
    ok, _, message = registry.adopt(
        directory, approve=lambda plugin, f: called.append(True) or True)
    assert ok is False
    assert "security gate error" in message
    assert "scanner exploded" in message
    assert called == [], "a failed gate must never reach an approval card"
    assert not (home / "plugins" / "upper").exists()


def test_gate_per_file_oserror_still_skips_and_adopts(tmp_path, monkeypatch):
    """The inner per-file OSError continue is unchanged behaviour."""
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)

    def unreadable_gate(source, text, client=None):
        raise OSError("cannot read")

    monkeypatch.setattr(security_gate, "gate_text", unreadable_gate)
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True)
    assert ok is True
    assert (home / "plugins" / "upper" / "plugin.json").exists()


class _FailingSandbox:
    def __init__(self, stderr):
        self.stderr = stderr

    def run(self, command):
        return SimpleNamespace(ok=False, stderr=self.stderr, stdout="")


def _shell_error(tmp_path, network_available, stderr="curl: could not resolve host"):
    work = tmp_path / "work"
    kwargs = {} if network_available is None else {
        "network_available": network_available}
    registry = ToolRegistry(work, sandbox=_FailingSandbox(stderr), **kwargs)
    with pytest.raises(ValueError) as excinfo:
        registry.call("shell", {"command": ["curl", "https://example.invalid"]})
    return str(excinfo.value)


def test_offline_shell_failure_gets_hint_but_keeps_prefix_and_cap(tmp_path):
    long_stderr = "e" * 300
    message = _shell_error(tmp_path, False, stderr=long_stderr)
    assert message.startswith("shell failed: ")
    assert "e" * 200 in message
    assert "e" * 201 not in message, "stderr stays capped at 200 chars"
    assert "no network" in message
    assert "web_search" in message
    assert "fetch_url" in message


def test_shell_failure_unchanged_when_network_true_or_unknown(tmp_path):
    for network_available in (True, None):
        message = _shell_error(tmp_path, network_available,
                               stderr="boom: something broke")
        assert message == "shell failed: boom: something broke"


def test_tool_registry_default_construction_still_works(tmp_path):
    # Existing constructions (no network_available arg) must keep working.
    registry = ToolRegistry(tmp_path / "work")
    assert registry.network_available is None
    assert registry.call("write", {"file": "a.txt", "text": "hi"}) == "wrote a.txt"


# --- next lifecycle test module ---

"""Integration wiring for the Lifecycle batch, done by the parent:
_network_available in main.py and Slack image markers in the WebUI
Slack handler. These are the two cross-agent seams no single part
owned, so they get their own tests here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from src.system.main import _network_available


class _Docker:
    def __init__(self, allow):
        self.allow_network = allow


class _Bwrap:
    pass


class _Sandbox:
    def __init__(self, bwrap):
        self.bwrap = bwrap


def test_network_available_docker_flag():
    assert _network_available(_Docker(True), _Sandbox(True)) is True
    assert _network_available(_Docker(False), _Sandbox(False)) is False


def test_network_available_bwrap_backend_is_offline():
    assert _network_available(_Bwrap(), _Sandbox(True)) is False


def test_network_available_no_backend_follows_sandbox():
    assert _network_available(None, _Sandbox(True)) is False
    assert _network_available(None, _Sandbox(None)) is True


def test_slack_handler_attaches_image_markers(tmp_path):
    from src.interface.webui import WebUIServer

    seen = []

    class _Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def dispatch(self, event):
            seen.append(event)

    server = WebUIServer(tmp_path, dispatcher=_Dispatcher())
    body = {"event": {"type": "message", "text": "what is this",
                      "channel": "C1", "user": "U1",
                      "files": [{"id": "F1", "mimetype": "image/png",
                                 "name": "photo.png", "filetype": "png"}]}}
    status, _payload = server.handle_slack_post(b"", {}, body)
    assert status == 200
    assert len(seen) == 1
    assert "[image slack_file_id=F1" in seen[0].goal
    assert seen[0].payload["images"][0]["id"] == "F1"


def test_slack_handler_plain_text_unchanged(tmp_path):
    from src.interface.webui import WebUIServer

    seen = []

    class _Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def dispatch(self, event):
            seen.append(event)

    server = WebUIServer(tmp_path, dispatcher=_Dispatcher())
    body = {"event": {"type": "message", "text": "hello",
                      "channel": "C1", "user": "U1"}}
    status, _payload = server.handle_slack_post(b"", {}, body)
    assert status == 200
    assert seen[0].goal == "hello"
    assert seen[0].payload == {"force_web": False, "think_max": False,
                               "research_mode": False, "mode": "ask",
                               "channel": "C1",
                               "thread_ts": None}


# --- next lifecycle test module ---

"""Research, HTML stripping, Slack image intake, and report tiers.

Covers the combined youtube_learn contract, the shared strip_html
contract on both the KB and research paths, Slack image intake, and
per-tier router counts surfacing in autonomy_report.
"""
import json

from src.agentic.autonomy_metrics import (
    ROUTER_FAST_CALL,
    ROUTER_STRONG_CALL,
    AutonomyMetrics,
)
from src.agentic.research import Researcher, _strip_html
from src.interface.report import autonomy_report
from src.interface.slack_inbound import parse_slack_event
from src.memory.kb import extract_text, strip_html
from src.social.youtube import youtube_learn


# -- YouTube learn -----------------------------------------------------

def _fake_search(url):
    assert "youtube/v3/search" in url
    return {"items": [{"id": {"videoId": "vid1"},
                       "snippet": {"title": "Roast Beef",
                                   "channelTitle": "Kitchen"}}]}


_XML = ('<transcript><text start="0" dur="1">Hello</text>'
        '<text start="1" dur="1">world</text></transcript>')


def test_youtube_learn_query_combines_metadata_and_transcript():
    result = youtube_learn(query="roast beef", api_key="k",
                           get=_fake_search, fetch=lambda url: _XML)
    assert result["ok"] is True
    assert result["metadata"]["video_id"] == "vid1"
    assert result["metadata"]["title"] == "Roast Beef"
    assert result["transcript"] == "Hello world"
    assert result["chapters"] == []


def test_youtube_learn_no_transcript_says_so_plainly():
    result = youtube_learn(video_id="vid9", fetch=lambda url: "")
    assert result["ok"] is True
    assert result["transcript"] is None
    assert result["transcript_available"] is False
    assert "No transcript available" in result["transcript_note"]
    assert result["metadata"]["video_id"] == "vid9"


def test_youtube_learn_visual_request_refused_not_faked():
    result = youtube_learn(query="x", api_key="k", visual_request=True)
    assert result["ok"] is False
    assert "text-only" in result["refusal"]
    assert result["transcript"] is None


def test_youtube_learn_needs_query_or_video_id():
    result = youtube_learn()
    assert result["ok"] is False
    assert result["transcript"] is None


# -- HTML stripping, shared --------------------------------------------

_HTML = """<html><head><style>.x{color:red}</style>
<script>var evil = 1;</script>
<script type="application/ld+json">{"name": "Roast Beef", "price": "12"}</script>
</head><body><p>Intro text</p>
<table><tr><th>Item</th><th>Price</th></tr>
<tr><td>Beef</td><td>$12</td></tr>
<tr><td>Potatoes</td><td>$3</td></tr></table>
</body></html>"""


def test_strip_html_removes_script_style_lifts_jsonld_keeps_rows():
    text = strip_html(_HTML)
    assert "evil" not in text
    assert "color:red" not in text
    assert "Roast Beef" in text  # lifted from JSON-LD
    assert "Intro text" in text
    lines = text.splitlines()
    assert "Item Price" in lines
    assert "Beef $12" in lines
    assert "Potatoes $3" in lines


def test_kb_and_research_share_the_same_stripper(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(_HTML, encoding="utf-8")
    assert extract_text(html_file) == strip_html(_HTML)
    assert _strip_html(_HTML) == strip_html(_HTML)


class _Cache:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, url):
        return self.raw


def test_research_excerpt_keeps_rows_and_claims_still_match():
    researcher = Researcher(cache=_Cache(_HTML))
    source = researcher.fetch("http://example.test/page")
    assert source.error == ""
    assert "Beef $12" in source.excerpt.splitlines()
    report = researcher.research("price?", ["http://example.test/page"],
                                 claims=["beef $12"])
    assert report.verified_facts, "claim split across lines must still verify"


# -- Slack image intake -------------------------------------------------

def _slack_payload(text="", files=None, file_shares=None):
    event = {"type": "message", "channel": "C1", "user": "U1", "text": text}
    if files is not None:
        event["files"] = files
    if file_shares is not None:
        event["file_shares"] = file_shares
    return {"event": event}


def test_slack_image_attached_like_whatsapp_marker():
    payload = _slack_payload(
        "what is this?", files=[
            {"id": "F1", "name": "plant.jpg", "mimetype": "image/jpeg",
             "url_private": "https://files.slack.test/F1"},
            {"id": "F2", "name": "notes.txt", "mimetype": "text/plain",
             "url_private": "https://files.slack.test/F2"},
        ])
    event = parse_slack_event(payload)
    assert event is not None
    assert "[image slack_file_id=F1" in event.goal
    assert "[file slack_file_id=F2" in event.goal  # documents attach too
    assert len(event.payload["images"]) == 1
    assert event.payload["images"][0]["name"] == "plant.jpg"
    assert event.payload["images"][0]["downloaded"] is False
    assert len(event.payload["files"]) == 1
    assert event.payload["files"][0]["name"] == "notes.txt"


def test_slack_image_only_message_still_becomes_goal():
    payload = _slack_payload(file_shares=[
        {"id": "F3", "name": "pic.png", "mimetype": "image/png",
         "url_private": "https://files.slack.test/F3"}])
    event = parse_slack_event(payload)
    assert event is not None
    assert "slack_file_id=F3" in event.goal


def test_slack_text_only_unchanged_and_bot_ignored():
    event = parse_slack_event(_slack_payload("hello"))
    assert event is not None and event.goal == "hello"
    assert event.payload["images"] == []
    bot = {"event": {"type": "message", "bot_id": "B1", "text": "hi"}}
    assert parse_slack_event(bot) is None


# -- Report per-tier counts --------------------------------------------

def test_autonomy_report_surfaces_router_tiers(tmp_path):
    metrics = AutonomyMetrics(tmp_path)
    metrics.record(ROUTER_FAST_CALL, tokens=10)
    metrics.record(ROUTER_FAST_CALL, tokens=20)
    metrics.record(ROUTER_STRONG_CALL, tokens=100)
    report = autonomy_report(tmp_path)
    assert report["router_fast_calls"] == 2
    assert report["router_fast_tokens"] == 30
    assert report["router_strong_calls"] == 1
    assert report["router_strong_tokens"] == 100


def test_autonomy_report_without_metrics_file_is_zero(tmp_path):
    report = autonomy_report(tmp_path)
    assert report["router_fast_calls"] == 0
    assert report["router_strong_calls"] == 0
    assert report["router_fast_tokens"] == 0
    assert report["router_strong_tokens"] == 0


# --- next lifecycle test module ---

"""Swarm execution, budget sharing, and network state handling.
safety, adapted-DAG replan routing, sandbox network-on contract.
"""
import json
import threading

import pytest

import src.agentic.dag as dag_mod
import src.agentic.adapt as adapt_mod
from src.agentic.budgets import Budget
from src.agentic.loop import GoalRunner
from src.agentic.swarm import run_swarm
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


# ---- 1. swarm double-run -------------------------------------------------

def test_swarm_no_subgoals_never_calls_runner():
    calls = []
    result = run_swarm("Do one thing", FakeClient("SINGLE"),
                       lambda goal: calls.append(goal) or "single done")
    # The caller runs the goal once itself on this shape, so
    # run_swarm returns empty results and never calls runner.
    assert result == {"swarmed": False, "subgoals": [], "results": [],
                      "merged": None}
    assert calls == []


def test_swarm_empty_reply_never_calls_runner():
    calls = []
    result = run_swarm("Do one thing", FakeClient(""),
                       lambda goal: calls.append(goal) or "single done")
    assert result["swarmed"] is False
    assert result["results"] == [] and result["merged"] is None
    assert calls == []


def test_swarm_split_still_runs_subgoals():
    result = run_swarm("Do two things",
                       FakeClient("1. Research A\n2. Research B"),
                       lambda subgoal: f"Done {subgoal}")
    assert result["swarmed"] is True
    assert result["subgoals"] == ["Research A", "Research B"]
    assert result["results"] == ["Done Research A", "Done Research B"]
    assert "## Research A" in result["merged"]


# ---- 2. Budget thread safety ----------------------------------------------

def test_budget_defaults_unchanged():
    budget = Budget()
    assert (budget.max_tokens, budget.max_steps, budget.max_seconds) == (200000, 50, 3600)
    assert budget.to_dict()["tokens_used"] == 0


def test_budget_counts_survive_parallel_subruns():
    budget = Budget(max_tokens=10 ** 9, max_steps=10 ** 9, max_seconds=10 ** 9)
    budget.start()

    def work():
        for _ in range(250):
            budget.add_step()
            budget.add_tokens("x", exact=4)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.steps_used == 2000
    assert budget.tokens_used == 8000
    assert budget.to_dict()["steps_used"] == 2000


# ---- helpers for adapted-plan tests ----------------------------------------

class _FakePoint:
    def __init__(self, payload):
        self.payload = payload
        self.text = payload.get("goal", "")
        self.id = "exp-1"


class _FakeHit:
    def __init__(self, point):
        self.point = point
        self.score = 1.0


class _FakeMemory:
    """recall() returns one experience; the rest of the
    memory surface is stubbed so a successful run completes."""

    def __init__(self, payload):
        self._payload = payload

    def recall(self, collection, query, limit=1, graph=False):
        return [_FakeHit(_FakePoint(self._payload))]

    def record_episodic(self, project_id, goal, data):
        return object()

    def auto_promote_enabled(self):
        return False


def _experience_payload(goal, nodes):
    return {"goal": goal, "goal_shape": goal, "dag_nodes": json.dumps(nodes),
            "tools_used": [], "skills_used": [], "outcome": "done",
            "lesson": "", "generalized_nodes": "[]"}


def _runner(tmp_path, memory=None, researcher=None, client=None, network_available=None):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved", "test"))
    return GoalRunner(client or FakeClient(""),
                      Sandbox(tmp_path / "work"), gate, tmp_path,
                      memory=memory, researcher=researcher,
                      network_available=network_available)


def _plan_json(filename):
    return json.dumps({"steps": [{"tool": "write",
                                  "args": {"file": filename, "text": "hi"},
                                  "deliverable": filename}],
                       "deliverables": [filename], "criteria": []})


# ---- 3. adapted-DAG replan routing ------------------------------------------

def test_adapted_dag_plan_goes_through_replan(tmp_path):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or [])
    seen = []
    original = runner._research_and_replan

    def spy(plan, goal_arg, spec=None):
        seen.append(plan)
        return original(plan, goal_arg, spec=spec)

    runner._research_and_replan = spy
    report = runner.run(goal)
    # The adapted plan reached the research+replan pass, even
    # though it carries no open questions (so the researcher
    # is correctly not called and the plan is unchanged).
    assert len(seen) == 1
    assert seen[0].deliverables == ["a.md"]
    assert researcher_calls == []
    assert report.status == "done"
    assert "a.md" in report.files


def test_adapted_dag_plan_with_needs_research_gets_research(tmp_path, monkeypatch):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    real_adapt = dag_mod.adapt_dag

    def adapt_with_questions(experience, new_goal, client=None):
        dag, summary, model_calls = real_adapt(experience, new_goal, client=client)
        return dag, {**summary, "needs_research": ["price of X"]}, model_calls

    monkeypatch.setattr(dag_mod, "adapt_dag", adapt_with_questions)
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or ["note"],
                     client=FakeClient(_plan_json("b.md")))
    report = runner.run(goal)
    assert researcher_calls == ["price of X"]
    # The replanned plan (b.md), not the adapted plan (a.md), ran.
    assert report.status == "done"
    assert "b.md" in report.files
    assert (tmp_path / "work" / "b.md").exists()
    assert report.adapted is True


def test_adapted_experience_nodes_with_needs_research_get_research(tmp_path, monkeypatch):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    monkeypatch.setattr(dag_mod, "adapt_dag", lambda *a, **k: None)
    monkeypatch.setattr(adapt_mod, "adapt_experience",
                        lambda experience, new_goal, client=None: (
                            [{"tool": "write", "args": {"file": "c.md", "text": "hi"},
                              "deliverable": "c.md",
                              "needs_research": ["auth shape of Y"]}], 1, 0))
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or ["note"],
                     client=FakeClient(_plan_json("d.md")))
    report = runner.run(goal)
    assert researcher_calls == ["auth shape of Y"]
    assert report.status == "done"
    assert "d.md" in report.files
    assert (tmp_path / "work" / "d.md").exists()


# ---- 4. sandbox network-on contract ------------------------------------------

def _run_simple_goal(tmp_path, network_available):
    bodies = []
    gate = ApprovalGate(lambda kind, title, body: bodies.append(body) or Decision("approved", "test"))
    runner = GoalRunner(FakeClient(_plan_json("e.md")),
                        Sandbox(tmp_path / "work"), gate, tmp_path,
                        network_available=network_available)
    return runner, bodies


def test_network_on_announces_reports_and_marks_card(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, True)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is True
    assert "Sandbox network: on" in capsys.readouterr().err
    assert any("Sandbox network: on" in body for body in bodies)


def test_network_none_says_nothing(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, None)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is None
    assert "Sandbox network: on" not in capsys.readouterr().err
    assert all("Sandbox network: on" not in body for body in bodies)


def test_network_false_says_nothing(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, False)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is False
    assert "Sandbox network: on" not in capsys.readouterr().err
    assert all("Sandbox network: on" not in body for body in bodies)


def test_network_default_is_unknown_and_registry_kwarg_optional(tmp_path):
    # ToolRegistry does not accept network_available yet (a
    # sibling change owns it); constructing a runner with a
    # network value must still work, with the value stored.
    runner, _ = _run_simple_goal(tmp_path, True)
    assert runner.network_available is True
    assert runner.tools is not None


# --- next lifecycle test module ---

"""Goal control, guidance, initial planning, tracing, and DAG execution.
budget failures, dag-run edges, guidance, kernel notice, trace
attribution.

main.py branch wiring (register + print) is covered by the dag-run
tests below, which drive main() end to end; the loop-side contract
(run_id + run_control actually gating a run) is covered by the
cancel test.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, "src")

from src.agentic.budgets import Budget
from src.agentic.loop import GoalRunner
from src.agentic.trace import TraceRecorder
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.run_control import RunControl
from src.system.model_client import FakeClient

PLAN_JSON = json.dumps({
    "steps": [{"tool": "write", "args": {"file": "a.txt", "text": "hi"},
               "deliverable": "a.txt"}],
    "deliverables": ["a.txt"],
    "criteria": ["a.txt exists"],
})


def _runner(tmp_path, client=None, **kwargs):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(client or FakeClient(PLAN_JSON),
                      Sandbox(tmp_path / "work", 120), gate, tmp_path,
                      **kwargs)


def test_fresh_plan_budget_breach_fails_run_not_crash(tmp_path):
    runner = _runner(tmp_path, budget=Budget(max_tokens=0, max_steps=50,
                                             max_seconds=3600))
    report = runner.run("write a.txt")
    assert report.status == "failed"
    assert "budget exceeded" in (report.error or "")


def test_spec_plan_budget_breach_fails_run_not_crash(tmp_path):
    runner = _runner(tmp_path, budget=Budget(max_tokens=0, max_steps=50,
                                             max_seconds=3600))
    report = runner.run("write x.txt", spec={"deliverables": ["x.txt"]})
    assert report.status == "failed"
    assert "budget exceeded" in (report.error or "")
    assert report.plan.deliverables == ["x.txt"]


def test_guidance_reaches_plan_prompt(tmp_path):
    client = FakeClient(PLAN_JSON)
    runner = _runner(tmp_path, client=client, guidance="Always paint it blue")
    plan = runner._plan("write a report")
    assert plan.deliverables == ["a.txt"]
    user_message = client.calls[0][1].content
    assert "Always paint it blue" in user_message
    assert "binding" in user_message


def test_guidance_empty_adds_nothing(tmp_path):
    client = FakeClient(PLAN_JSON)
    runner = _runner(tmp_path, client=client)
    runner._plan("write a report")
    assert "binding" not in client.calls[0][1].content


def test_kernel_notice_on_stderr_in_kernel_mode(tmp_path, capsys):
    runner = _runner(tmp_path, kernel_mode=True,
                     budget=Budget(max_tokens=0, max_steps=50,
                                   max_seconds=3600))
    runner.run("write a.txt")
    assert "Kernel mode on: only seed tools" in capsys.readouterr().err


def test_no_kernel_notice_outside_kernel_mode(tmp_path, capsys):
    runner = _runner(tmp_path, kernel_mode=False,
                     budget=Budget(max_tokens=0, max_steps=50,
                                   max_seconds=3600))
    runner.run("write a.txt")
    assert "Kernel mode on" not in capsys.readouterr().err


def test_cancelled_registered_run_reports_cancelled(tmp_path):
    control = RunControl(tmp_path)
    assert control.register("run-1", "write a.txt", "default")
    assert control.cancel("run-1") is True
    runner = _runner(tmp_path, run_id="run-1", run_control=control)
    report = runner.run("write a.txt")
    assert report.status == "cancelled"


def test_trace_attribution_shared_recorder(tmp_path):
    recorder = TraceRecorder(tmp_path, project_id="default")
    parent = _runner(tmp_path, trace=recorder)
    sub = _runner(tmp_path, trace=recorder, run_id="sub-abc")
    parent._trace("sandbox", "started", "parent step")
    sub._trace("sandbox", "started", "sub step")
    parent_file = recorder.trace_path.read_text()
    sub_file = (tmp_path / "trace" / "sub-abc.jsonl").read_text()
    assert "parent step" in parent_file
    assert "sub step" not in parent_file
    assert "sub step" in sub_file
    assert "parent step" not in sub_file


def test_trace_no_run_id_falls_back_to_recorder(tmp_path):
    recorder = TraceRecorder(tmp_path, project_id="default")
    runner = _runner(tmp_path, trace=recorder)
    assert runner.run_id is None
    runner._trace("sandbox", "started", "fallback step")
    assert "fallback step" in recorder.trace_path.read_text()


def _dag_home(tmp_path, monkeypatch, nodes):
    from src.agentic.dag_store import DagStore
    home = tmp_path / "home"
    monkeypatch.setenv("AIDON_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    store = DagStore(home)
    store.save("d1", "test dag", nodes)
    return home


def _write_node(node_id, file, text="hi"):
    return {"id": node_id, "tool": "write", "args": {"file": file, "text": text},
            "deliverable": file, "depends_on": [], "executor": "tool"}


def test_dag_run_via_main(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch, [_write_node("n1", "hello.txt")])
    assert main(["dag", "run", "d1"]) == 0
    assert (home / "workspace" / "hello.txt").read_text() == "hi"


def test_dag_run_absolute_workdir_path_relativized(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch, [])
    workdir = home / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    from src.agentic.dag_store import DagStore
    DagStore(home).save("d1", "test dag",
                        [_write_node("n1", str(workdir / "abs.txt"))])
    assert main(["dag", "run", "d1"]) == 0
    assert (workdir / "abs.txt").read_text() == "hi"


def test_dag_run_duplicate_ids_refused(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch,
                     [_write_node("n1", "a.txt"),
                      _write_node("n1", "b.txt")])
    assert main(["dag", "run", "d1"]) == 2
    assert not (home / "workspace" / "a.txt").exists()
    assert not (home / "workspace" / "b.txt").exists()


def test_dag_relativize_unit(tmp_path):
    from src.agentic.dag import DAG, DAGNode
    workdir = str(tmp_path / "work")
    dag = DAG([DAGNode(id="n1", tool="write",
                       args={"file": f"{workdir}/hello.txt"},
                       deliverable="hello.txt")])
    assert dag.relativize(workdir).nodes["n1"].args["file"] == "hello.txt"


# --- next lifecycle test module ---

"""Approval callback binding and Slack download behavior.

The gate.ask adopt path already bound approval to a tree hash; the
approve-callback path did not show the approver what it approved.
And Slack file bytes had no download helper at all (markers +
metadata only). Both are covered here, without wiring the helper
into any handler.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from src.capabilities.plugins import PluginRegistry, plugin_tree_hash
from src.interface.messaging import download_slack_file


def _plugin_dir(tmp_path, name="upper"):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": []}), encoding="utf-8")
    (directory / "tool.py").write_text(
        "def upper(args):\n    return str(args.get('text', '')).upper()\n",
        encoding="utf-8")
    return directory


def test_callback_receives_hash_and_body(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    seen = {}

    def approve(plugin, findings, tree_hash, body):
        seen["hash"] = tree_hash
        seen["body"] = body
        seen["name"] = plugin.name
        return True

    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(directory, approve=approve)
    assert ok is True
    assert seen["hash"] == plugin_tree_hash(directory)
    assert seen["name"] == "upper"
    assert f"Approved source hash: {seen['hash']}" in seen["body"]
    assert "Plugin: upper 1.0.0" in seen["body"]


def test_callback_old_arity_still_works(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(
        directory, approve=lambda plugin, findings: True)
    assert ok is True
    assert (home / "plugins" / "upper" / "tool.py").is_file()


def test_callback_rejection_does_not_copy(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(
        directory, approve=lambda plugin, findings, h, b: False)
    assert ok is False
    assert not (home / "plugins" / "upper").exists()


def test_download_private_url_via_fake_get():
    calls = []

    def fake_get(url, token):
        calls.append((url, token))
        return b"PNGDATA"

    data = download_slack_file(
        "xoxb-test-token", "https://files.slack.com/files-pri/T1/F1/photo.png",
        get=fake_get)
    assert data == b"PNGDATA"
    assert calls == [("https://files.slack.com/files-pri/T1/F1/photo.png",
                      "xoxb-test-token")]
    assert "xoxb-test-token" not in calls[0][0]


def test_download_file_id_resolves_via_files_info():
    calls = []

    def fake_get(url, token):
        calls.append(url)
        if "files.info" in url:
            assert "token=" not in url
            return {"ok": True,
                    "file": {"url_private": "https://files.slack.com/files-pri/T1/F1/photo.png"}}
        return b"BYTES"

    data = download_slack_file("xoxb-test-token", "F1ABC", get=fake_get)
    assert data == b"BYTES"
    assert calls[0].startswith("https://slack.com/api/files.info?file=F1ABC")
    assert calls[1] == "https://files.slack.com/files-pri/T1/F1/photo.png"


def test_download_refuses_non_https_before_any_request():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"SHOULD-NOT-BE-FETCHED")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/photo.png"
        with pytest.raises(ValueError, match="non-https"):
            download_slack_file("xoxb-test-token", url)
        assert hits == []
        with pytest.raises(ValueError, match="non-https"):
            download_slack_file("xoxb-test-token", "ftp://example.com/x.png")
    finally:
        server.shutdown()
        server.server_close()


def test_download_refuses_empty_token_or_url():
    with pytest.raises(ValueError):
        download_slack_file("", "https://files.slack.com/x.png")
    with pytest.raises(ValueError):
        download_slack_file("xoxb-test-token", "")


def test_download_errors_never_leak_token():
    token = "xoxb-secret-token-123"

    def failing_get(url, token_arg):
        raise ValueError("upstream boom")

    with pytest.raises(ValueError) as excinfo:
        download_slack_file(token, "https://files.slack.com/x.png",
                            get=failing_get)
    assert token not in str(excinfo.value)

    def no_url_get(url, token_arg):
        return {"ok": True, "file": {}}

    with pytest.raises(ValueError) as excinfo:
        download_slack_file(token, "F999", get=no_url_get)
    assert token not in str(excinfo.value)

    def dict_get(url, token_arg):
        return {"ok": True, "not": "bytes"}

    with pytest.raises(ValueError, match="did not return bytes"):
        download_slack_file(token, "https://files.slack.com/x.png",
                            get=dict_get)


# --- next lifecycle test module ---

"""Trace attribution, concurrency, and DAG validation.

Trace: record() is lock-guarded and accepts an optional run_id
override, so parallel sub-runs sharing one recorder no longer
interleave lines or misattribute events to the parent run.
DAG: __init__ still keeps the last node per id, but duplicate
ids are recorded and validate() reports them; validate_strict()
raises. Executor tiers are intentionally untouched (see the
last test): tool/fast/strong routing is a designed feature.
"""
import json
import threading

import pytest

from src.agentic.dag import DAG, DAGNode, route_executor
from src.agentic.trace import TraceRecorder


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_default_unchanged(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    event = rec.record("plan", "done", "hello")
    assert event.run_id == "parent"
    assert rec.kinds() == ["plan"]
    events = _read_events(tmp_path / "trace" / "parent.jsonl")
    assert len(events) == 1 and events[0]["run_id"] == "parent"


def test_record_run_id_override_attributes_to_subrun(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    event = rec.record("sandbox", "done", "sub work", run_id="child-1")
    assert event.run_id == "child-1"
    # The sub-run's event lands in the sub-run's file, not the parent's.
    assert _read_events(tmp_path / "trace" / "child-1.jsonl")[0]["run_id"] == "child-1"
    assert _read_events(tmp_path / "trace" / "parent.jsonl") == []
    # And the shared in-memory list still holds it.
    assert rec.events[-1].run_id == "child-1"


def test_record_parallel_threads_no_lost_or_corrupt_lines(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    threads = [
        threading.Thread(target=lambda i=i: [
            rec.record("sandbox", "done", f"step {i}-{j}", run_id=f"sub-{i}")
            for j in range(25)])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rec.events) == 200
    for i in range(8):
        events = _read_events(tmp_path / "trace" / f"sub-{i}.jsonl")
        assert len(events) == 25
        assert all(e["run_id"] == f"sub-{i}" for e in events)


def test_kinds_snapshot_under_lock(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    rec.record("plan", "done")
    rec.record("sandbox", "done")
    assert rec.kinds() == ["plan", "sandbox"]


def test_dag_init_still_keeps_last_node_but_flags_duplicate():
    first = DAGNode(id="a", tool="write", args={"file": "one.py"})
    second = DAGNode(id="a", tool="write", args={"file": "two.py"})
    dag = DAG([first, second])
    # __init__ behaviour unchanged: one entry, last wins.
    assert list(dag.nodes) == ["a"]
    assert dag.nodes["a"].args == {"file": "two.py"}
    # But validation no longer stays silent about it.
    assert any("duplicate node id: a" in f for f in dag.validate())


def test_dag_validate_strict_raises_on_duplicate():
    dag = DAG([DAGNode(id="a"), DAGNode(id="a", tool="read")])
    with pytest.raises(ValueError, match="duplicate node id: a"):
        dag.validate_strict()


def test_dag_validate_strict_passes_clean_and_cycle_still_loud():
    clean = DAG([DAGNode(id="a"), DAGNode(id="b", depends_on=["a"])])
    assert clean.validate() == []
    assert clean.validate_strict() is None
    cyclic = DAG([DAGNode(id="a", depends_on=["b"]),
                  DAGNode(id="b", depends_on=["a"])])
    assert any("cycle" in f for f in cyclic.validate())
    with pytest.raises(ValueError, match="cycle"):
        cyclic.validate_strict()


def test_dag_add_still_raises_on_duplicate():
    dag = DAG([DAGNode(id="a")])
    with pytest.raises(ValueError, match="duplicate node id"):
        dag.add(DAGNode(id="a"))


def test_executor_tiers_untouched():
    # Deliberately not a gap: tool/fast/strong is a designed tier
    # feature, and `dag run` refusing non-tool nodes does not
    # change how the loop routes them. This test pins the tiers.
    assert route_executor(DAGNode(id="a", executor="fast")) == "fast"
    assert route_executor(DAGNode(id="b", executor="strong")) == "strong"
    assert route_executor(DAGNode(id="c", executor="tool")) == "tool"
    assert route_executor(DAGNode(id="d", executor="bogus")) == "tool"


# --- next lifecycle test module ---

"""Main entry-point branches and DAG command behavior.

Covers what can be unit-tested without running the CLI: the CLI gate
timeout threading, declare_blocked's direction-backed string shape, and
the dag run wiring (executor + tool registry + Registry checkpoints)
that `aidon dag run` uses. The serve-project, interview-runner, and
swarm-runner branch edits are verified by inspection of the branch
code (they live inside main() and need a full CLI run to execute);
the assertions below pin the helpers those branches depend on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.dag import DAG, DAGNode
from src.agentic.dag_executor import DAGExecutor
from src.agentic.direction import blocked_note
from src.capabilities.registry_db import Registry
from src.capabilities.tools import ToolRegistry
from src.system.main import _cli_gate, _goal_extra_tools


class _Config:
    github_token = ""
    threads_account_id = ""
    threads_access_token = ""
    ig_user_id = ""
    ig_access_token = ""
    fish_api_key = ""
    tts_voice_id = ""
    transcribe_api_key = ""
    owner_email = "owner@example.com"


def _tools(tmp_path, **kw):
    cfg = _Config()
    cfg.home = tmp_path
    return _goal_extra_tools(cfg, tmp_path / "work", **kw)


def test_cli_gate_default_timeout_is_900(tmp_path):
    assert _cli_gate(tmp_path).timeout_seconds == 900


def test_cli_gate_accepts_configured_timeout(tmp_path):
    assert _cli_gate(tmp_path, 42).timeout_seconds == 42


def test_declare_blocked_keeps_exact_string_shape():
    tools = _tools(Path("/tmp/lifecycle-test-home"))
    assert tools["declare_blocked"]("no network") == "[blocked] no network"


def test_declare_blocked_matches_direction_blocked_note():
    tools = _tools(Path("/tmp/lifecycle-test-home"))
    for reason in ("stuck", "", "needs a key"):
        assert tools["declare_blocked"](reason) == f"[blocked] {blocked_note(reason)['reason']}"


def test_dag_run_tool_nodes_execute_in_order(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "hello.txt", "text": "hi"}),
        DAGNode(id="b", tool="read", args={"file": "hello.txt"}, depends_on=["a"]),
    ])
    result = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args)).execute()
    assert result["status"] == "done"
    assert result["completed"] == ["a", "b"]
    assert result["results"]["b"] == "hi"


def test_dag_run_unknown_tool_fails_without_rollback(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "kept.txt", "text": "kept"}),
        DAGNode(id="b", tool="nope", args={}, depends_on=["a"]),
    ])
    result = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args)).execute()
    assert result["status"] == "failed"
    assert result["completed"] == ["a"]
    assert (workdir / "kept.txt").read_text() == "kept"


def test_dag_run_resume_skips_checkpointed_nodes(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "x.txt", "text": "x"}),
        DAGNode(id="b", tool="write", args={"file": "y.txt", "text": "y"}, depends_on=["a"]),
    ])
    state = Registry(tmp_path / "home")
    first = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args),
                        state=state, run_id="resume-1")
    # Simulate a killed run: checkpoint only node a, then resume.
    first._set_checkpoint("a", "wrote x.txt")
    calls = []
    def counting(node):
        calls.append(node.id)
        return registry.call(node.tool, node.args)
    resumed = DAGExecutor(dag, counting, state=state, run_id="resume-1").execute()
    assert resumed["status"] == "done"
    assert calls == ["b"]
    assert resumed["completed"] == ["a", "b"]

