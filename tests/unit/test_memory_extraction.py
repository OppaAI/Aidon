"""Background fact extraction + retention scoring.

Extraction: one strong-model call per chat turn pair (or per whole
agentic run) returning facts + understandings, either list possibly
empty, recorded into episodic/semantic by scope. Retention: recall
re-ranks by vector score + 0.25 x extraction importance, so an
important fact can win the plan prompt's episode slot.
"""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan, RunReport
from src.agentic.router import RoutedClient
from src.memory.embedder import HashEmbedder
from src.memory.extraction import (
    _strong_chat,
    extraction_enabled,
    parse_extraction,
    record_extraction,
    spawn_extraction,
)
from src.memory.memory import Hit, Memory, InMemoryStore, retention_rank
from src.system.model_client import FakeClient


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def _texts(memory, collection):
    return [p.text for p in memory.list_points(collection)]


# --- parsing ---

def test_parse_strict_json():
    out = parse_extraction(json.dumps({
        "facts": [{"subject": "User", "predicate": "prefers",
                   "value": "roast beef", "scope": "semantic",
                   "importance": 0.9}],
        "understandings": [{"text": "User plans a dinner for two",
                            "scope": "episodic", "importance": 0.7}]}))
    assert out["facts"][0]["value"] == "roast beef"
    assert out["facts"][0]["importance"] == 0.9
    assert out["understandings"][0]["text"] == "User plans a dinner for two"


def test_parse_fallback_block_and_garbage():
    out = parse_extraction(
        'Thinking out loud {"facts": [{"subject": "A", "value": "B"}'
        '], "understandings": []} done')
    assert out["facts"][0]["subject"] == "A"
    assert out["facts"][0]["scope"] == "episodic"  # default scope
    assert out["facts"][0]["importance"] == 0.5  # default importance
    assert parse_extraction("no json here") == {"facts": [],
                                                "understandings": []}
    assert parse_extraction("") == {"facts": [], "understandings": []}


def test_parse_drops_malformed_and_clamps_importance():
    out = parse_extraction(json.dumps({
        "facts": [{"subject": "", "value": "x"},
                  {"subject": "A", "value": "B", "importance": 5},
                  {"subject": "C", "value": "D", "importance": -2},
                  {"subject": "E", "value": "F", "importance": "junk"},
                  "not a dict"],
        "understandings": [{"text": ""}, {"text": "Keep me"}]}))
    assert [f["subject"] for f in out["facts"]] == ["A", "C", "E"]
    assert [f["importance"] for f in out["facts"]] == [1.0, 0.0, 0.5]
    assert [u["text"] for u in out["understandings"]] == ["Keep me"]


# --- recording ---

def test_record_extraction_by_scope(tmp_path):
    memory = _memory(tmp_path)
    count = record_extraction(memory, "proj", {
        "facts": [{"subject": "User", "predicate": "prefers",
                   "value": "roast beef", "scope": "episodic",
                   "importance": 0.8},
                  {"subject": "User", "predicate": "is allergic to",
                   "value": "milk", "scope": "semantic", "importance": 0.9}],
        "understandings": [{"text": "User plans a proposal dinner",
                            "scope": "semantic", "importance": 0.7}]})
    assert count == 3
    assert _texts(memory, "episodic") == ["User prefers roast beef"]
    assert _texts(memory, "semantic") == ["User is allergic to milk",
                                          "User plans a proposal dinner"]
    episodic = memory.list_points("episodic")[0]
    assert episodic.payload["kind"] == "fact"
    assert episodic.payload["project_id"] == "proj"
    assert episodic.payload["importance"] == 0.8
    assert record_extraction(memory, "proj",
                             {"facts": [], "understandings": []}) == 0


# --- strong-model routing ---

def test_strong_chat_prefers_strong_tier():
    routed = RoutedClient(FakeClient("fast reply"), FakeClient("strong reply"))
    assert _strong_chat(routed, []) == "strong reply"
    assert _strong_chat(FakeClient("plain reply"), []) == "plain reply"


# --- background spawn ---

def test_spawn_records_and_disabled_skips(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    monkeypatch.delenv("AIDON_MEMORY_EXTRACT", raising=False)
    reply = json.dumps({
        "facts": [{"subject": "User", "predicate": "prefers",
                   "value": "tea", "scope": "episodic", "importance": 0.6}],
        "understandings": []})
    thread = spawn_extraction(memory, "default", "User: hi\nAssistant: hi",
                              FakeClient(reply))
    assert thread is not None
    thread.join(timeout=5)
    assert _texts(memory, "episodic") == ["User prefers tea"]
    monkeypatch.setenv("AIDON_MEMORY_EXTRACT", "0")
    assert not extraction_enabled()
    assert spawn_extraction(memory, "default", "User: hi\nAssistant: hi",
                            FakeClient(reply)) is None


# --- _finalize wiring ---

def _finalize_runner(tmp_path, memory):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = SimpleNamespace()
    runner.network_available = None
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner.memory = memory
    runner.project_id = "default"
    runner.run_id = "test"
    runner.home = tmp_path
    return runner


def test_finalize_spawns_for_chat_and_done_not_failed(tmp_path, monkeypatch):
    memory = _memory(tmp_path)
    calls = []
    monkeypatch.setattr("src.memory.extraction.spawn_extraction",
                        lambda *args: calls.append(args) or None)
    runner = _finalize_runner(tmp_path, memory)
    runner._finalize(RunReport("chat", "Plan dinner",
                               Plan(steps=[], deliverables=[], criteria=[]),
                               True, True, chat_reply="Roast beef it is"))
    assert len(calls) == 1
    assert "Plan dinner" in calls[0][2] and "Roast beef it is" in calls[0][2]
    calls.clear()
    runner._finalize(RunReport("done", "Write notes",
                               Plan(steps=[], deliverables=["a.md"],
                                    criteria=[]),
                               True, True, files=[]))
    assert len(calls) == 1
    assert "Goal: Write notes" in calls[0][2]
    assert "Deliverables: a.md" in calls[0][2]
    calls.clear()
    runner._finalize(RunReport("failed", "Broken goal",
                               Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, error="boom"))
    assert calls == []


# --- retention ranking ---

def _hit(text, score, payload):
    point = SimpleNamespace(text=text, payload=payload, id=text)
    return Hit(point, score)


def test_retention_rank_lifts_important_facts():
    hits = [
        _hit("plain episode", 0.99, {"kind": "episodic"}),
        _hit("unimportant fact", 0.80,
             {"kind": "fact", "importance": 0.1}),
        _hit("important fact", 0.70,
             {"kind": "fact", "importance": 0.9}),
    ]
    ranked = retention_rank(hits, 3)
    assert [h.point.text for h in ranked] == [
        "plain episode", "important fact", "unimportant fact"]
    assert [h.point.text for h in retention_rank(hits, 1)] == ["plain episode"]
    assert [h.point.text for h in retention_rank(hits, 2)] == [
        "plain episode", "important fact"]


class _FixedEmbedder:
    dim = 2

    def embed(self, text):
        return [1.0, 0.0]

    def embed_query(self, text):
        return [1.0, 0.0]

    def embed_sparse(self, text):
        return ([], [])


def test_recall_both_keeps_important_fact(tmp_path):
    memory = Memory(tmp_path / "home", embedder=_FixedEmbedder(),
                    store=InMemoryStore())
    record_extraction(memory, "default", {
        "facts": [{"subject": "Plain", "predicate": "is", "value": "first",
                   "scope": "episodic", "importance": 0.1},
                  {"subject": "Vital", "predicate": "is", "value": "second",
                   "scope": "episodic", "importance": 0.9}],
        "understandings": []})
    memory.record("episodic", "Ordinary episode note")
    episodes, _ = memory.recall_both("anything", episode_limit=1,
                                     knowledge_limit=0)
    assert len(episodes) == 1
    assert episodes[0].point.text == "Vital is second"


# --- plan prompt carries recalled facts ---

def test_plan_prompt_includes_episodic_and_semantic(tmp_path):
    from src.agentic.loop import GoalRunner as _GR

    class _ScriptedClient:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = []

        def chat(self, messages, sampling=None):
            self.calls.append(messages)
            return self.replies.pop(0)

    memory = _memory(tmp_path)
    record_extraction(memory, "default", {
        "facts": [{"subject": "User", "predicate": "prefers",
                   "value": "roast beef", "scope": "episodic",
                   "importance": 0.9},
                  {"subject": "User", "predicate": "is allergic to",
                   "value": "milk", "scope": "semantic", "importance": 0.9}],
        "understandings": []})
    plan_json = json.dumps({
        "steps": [{"tool": "write", "args": {"file": "a.txt"},
                   "deliverable": "a.txt"}],
        "deliverables": ["a.txt"], "criteria": [], "skills": []})
    client = _ScriptedClient([plan_json])
    runner = _GR.__new__(_GR)
    runner.client = client
    runner.router = None
    runner.plugins = SimpleNamespace(tool_names=lambda: [],
                                     skill_texts=lambda: {})
    runner.kernel_mode = False
    runner.extra_tools = {}
    runner.memory = memory
    runner.budget = None
    runner.guidance = ""
    runner.image_paths = []
    runner.stream_callback = None
    runner._effort_escalated = False
    runner._trace = lambda *args, **kwargs: None
    runner._plan("Plan a dinner menu")
    prompt = " ".join(str(m.content) for m in client.calls[0])
    assert "User prefers roast beef" in prompt
    assert "User is allergic to milk" in prompt
    assert "General facts" in prompt
