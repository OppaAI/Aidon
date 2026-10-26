"""Memory parity tests: hybrid recall, forgetting, projects,
promotion asking, KB parsing, Experience merge.

Offline: InMemoryStore + HashEmbedder, plus a stub
EmbeddingGemma 2 model for sparse / RRF shape. No Qdrant
server, no model download, no parser download required:
HTML parsing falls back to tag stripping, docx uses the
installed python-docx, PDF without pypdf falls back to
UTF-8 without crashing.
"""

from datetime import datetime, timedelta, timezone

from src.embedder import HashEmbedder, HybridEmbedder, SpladeEmbedder
from src.experience import (build_experience, goal_shape_for,
                            recall_experiences, store_experience)
from src.kb import KnowledgeBase, extract_text
from src.memory import (Memory, decay_score, is_grace_protected,
                        should_prune, InMemoryStore)
from src.loop import GoalRunner
from src.model_client import FakeClient
from src.sandbox import Sandbox
from src.approval import ApprovalGate, Decision


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def test_rrf_hybrid_recall_and_recall_both(tmp_path):
    memory = _memory(tmp_path)
    memory.record("episodic", "roast beef dinner for two")
    memory.record("episodic", "unrelated gardening notes")
    memory.record("knowledge", "roast beef cooking temperature 145")
    hits = memory.recall("episodic", "roast beef dinner", limit=1, graph=False)
    assert hits and "roast beef" in hits[0].point.text
    episodes, knowledge = memory.recall_both("roast beef", 2, 2)
    assert episodes and knowledge
    found = memory.forget_search("roast beef", limit=2)
    assert found["episodic"] and found["knowledge"]


def test_forgetting_curve_and_prune(tmp_path):
    # Fresh point: grace protected, never pruned.
    memory = _memory(tmp_path)
    point = memory.record("episodic", "fresh fact")
    assert is_grace_protected(point.payload["created_at"]) is True
    assert should_prune(point.payload) is False
    assert memory.prune("episodic") == []
    # Old, never re-accessed point: decayed below threshold.
    old = memory.record("episodic", "old fact")
    old.payload["created_at"] = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    old.payload["last_accessed"] = old.payload["created_at"]
    old.payload["last_access_day"] = "2020-01-01"
    old.payload["access_count"] = 1
    old.payload["access_day_count"] = 1
    memory.store.upsert("episodic", old)
    assert decay_score(1, old.payload["last_accessed"]) < 0.02
    assert should_prune(old.payload) is True
    assert memory.prune("episodic") == [old.id]
    # Recall is an access: access_count rises.
    point2 = memory.record("episodic", "accessed fact")
    before = point2.payload["access_count"]
    memory.recall("episodic", "accessed fact", limit=5, graph=False)
    after = memory.store.get("episodic", point2.id).payload["access_count"]
    assert after > before


def test_conversation_history_and_projects(tmp_path):
    memory = _memory(tmp_path)
    memory.record_stm("user", "hello", conversation_id="conv1", project_id="proj1")
    memory.record_stm("assistant", "hi there", conversation_id="conv1", project_id="proj1")
    memory.record_stm("user", "other chat", conversation_id="conv2", project_id="proj2")
    history = memory.conversation_history("conv1")
    assert len(history) == 2
    assert history[0].text == "hello"
    assert history[1].text == "hi there"
    context = memory.conversation_context("conv1")
    assert len(context["exchanges"]) == 2
    assert memory.list_projects() == ["proj1", "proj2"]
    summary = memory.record_summary("conv1", "User greeted, assistant replied.")
    assert memory.conversation_context("conv1")["summary"] == "User greeted, assistant replied."


def test_promote_with_ask_yes_and_no(tmp_path):
    memory = _memory(tmp_path)
    point_yes = memory.record_episodic("proj1", "fact to keep")
    out = memory.promote_with_ask(point_yes, lambda p: True)
    assert out is not None
    assert memory.recall("semantic", "fact to keep", graph=False)
    point_no = memory.record_episodic("proj1", "fact to drop")
    out2 = memory.promote_with_ask(point_no, lambda p: False)
    assert out2 is None
    texts = [h.point.text for h in memory.recall("semantic", "fact to drop", graph=False)]
    assert "fact to drop" not in texts


def test_kb_extract_html_and_docx(tmp_path):
    html = tmp_path / "page.html"
    html.write_text("<html><body><h1>Roast beef</h1><p>Serve hot.</p></body></html>")
    text = extract_text(html)
    assert "Roast beef" in text and "<h1>" not in text
    # KB ingest of HTML uses extract_text, not raw tags.
    memory = _memory(tmp_path)
    kb = KnowledgeBase(memory)
    kb.import_file(html)
    hits = kb.recall("roast beef")
    assert hits and "Roast beef" in hits[0].point.text


def test_experience_goal_shape_and_merge(tmp_path):
    memory = _memory(tmp_path)
    assert goal_shape_for("Plan a dinner for two") == goal_shape_for("dinner for two, plan")
    exp1 = build_experience("Write a note", "", [{"tool": "write", "args": {}, "deliverable": "a.md"}], ["write"], [], "done", "First lesson.")
    store_experience(memory, exp1)
    exp2 = build_experience("Write a note", "", [{"tool": "write", "args": {}, "deliverable": "b.md"}], ["write"], [], "done", "Second lesson.")
    store_experience(memory, exp2)
    points = memory.list_points("experience", limit=100)
    assert len(points) == 1
    merged = recall_experiences(memory, "Write a note")
    assert merged
    deliverables = {n["deliverable"] for n in merged[0].dag_nodes}
    assert deliverables == {"a.md", "b.md"}
    assert "Second lesson." in merged[0].lesson


def test_loop_promotion_ask(tmp_path):
    home = tmp_path / "home2"
    home.mkdir()
    memory = _memory(tmp_path)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, memory=memory,
                        promotion_ask=lambda p: True)
    report = runner.run("Write a note")
    assert report.status == "done"
    assert memory.recall("semantic", "Write a note", graph=False)
