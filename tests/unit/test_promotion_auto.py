"""Automatic LTM promotion: dedup on identical text, supersede
on same shape (>= half the shape tokens), record alongside
otherwise. No approval anywhere on this path.
"""

from src.memory.embedder import HashEmbedder
from src.memory.memory import InMemoryStore, Memory


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def _episodic(memory, text):
    return memory.record_episodic("default", text, {"kind": "run"})


def _semantic_texts(memory):
    return [p.text for p in memory.list_points("semantic", limit=100)]


def test_promote_records_then_dedups_identical(tmp_path):
    memory = _memory(tmp_path)
    point = _episodic(memory, "Write a note about cats")
    memory.promote_to_ltm(point, kind="semantic")
    assert _semantic_texts(memory) == ["Write a note about cats"]
    assert point.payload["promoted"] is True
    again = _episodic(memory, "Write a note about cats")
    memory.promote_to_ltm(again, kind="semantic")
    assert _semantic_texts(memory) == ["Write a note about cats"]


def test_promote_supersedes_same_shape(tmp_path):
    memory = _memory(tmp_path)
    memory.promote_to_ltm(_episodic(memory, "Write a note about cats"),
                          kind="semantic")
    memory.promote_to_ltm(_episodic(memory, "Write a note about dogs"),
                          kind="semantic")
    assert _semantic_texts(memory) == ["Write a note about dogs"]


def test_promote_keeps_unrelated_alongside(tmp_path):
    memory = _memory(tmp_path)
    memory.promote_to_ltm(_episodic(memory, "Write a note about cats"),
                          kind="semantic")
    memory.promote_to_ltm(_episodic(memory, "Deploy the observatory database"),
                          kind="semantic")
    assert sorted(_semantic_texts(memory)) == sorted([
        "Write a note about cats", "Deploy the observatory database"])


def test_promote_rejects_unknown_kind(tmp_path):
    memory = _memory(tmp_path)
    assert memory.promote_to_ltm(_episodic(memory, "Write a note"),
                                 kind="bogus") is None
    assert _semantic_texts(memory) == []


def test_chat_exchange_promotes_to_ltm(tmp_path):
    from types import SimpleNamespace

    from src.agentic.loop import GoalRunner, Plan, RunReport

    memory = _memory(tmp_path)
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
    report = RunReport("chat", "Hello there",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       True, True, chat_reply="Hi!")
    runner._finalize(report)
    assert _semantic_texts(memory) == ["Hello there"]
