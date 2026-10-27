"""Memory + KB + Experience framework tests, offline.

Uses the in-memory Qdrant-shaped store, so tests need no
Qdrant server and no embedding download. HashEmbedder is
deterministic, so recall is repeatable in tests. Real Qdrant
is used when qdrant_client is installed, via make_store(),
with the same collection / point / search shape.
"""

from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.memory.experience import build_experience, recall_experiences, store_experience
from src.memory.kb import KnowledgeBase, chunk_text
from src.agentic.loop import GoalRunner
from src.memory.memory import InMemoryStore, Memory
from src.core.model_client import FakeClient
from src.capabilities.sandbox import Sandbox


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def test_stm_episodic_semantic_collections_are_separate(tmp_path):
    memory = _memory(tmp_path)
    memory.record_stm("user", "hello, plan a dinner")
    assert len(memory.stm) == 1
    episodic = memory.record_episodic("proj1", "dinner for two, roast beef, no milk or eggs")
    assert memory.recall("episodic", "roast beef dinner")[0].point.id == episodic.id
    # Episodic recall does not leak into semantic until promoted.
    assert memory.recall("semantic", "roast beef dinner") == []
    promoted = memory.promote_to_ltm(episodic, kind="semantic")
    assert promoted is not None
    assert memory.recall("semantic", "roast beef dinner")[0].point.id == promoted.id
    assert memory.store.get("episodic", episodic.id).payload["promoted"] is True


def test_forget_and_forget_project(tmp_path):
    memory = _memory(tmp_path)
    point = memory.record_episodic("proj1", "secret project fact")
    assert memory.forget("episodic", point.id) is True
    assert memory.forget("episodic", point.id) is False
    memory.record_episodic("proj2", "another fact")
    assert memory.forget_project("proj2")["episodic"] == 1


def test_graph_expansion_follows_links(tmp_path):
    memory = _memory(tmp_path)
    target = memory.record("semantic", "roast beef cooking temperature", {})
    source = memory.record("semantic", "dinner main course", {"links": [target.id]})
    # Limit 1: only the source is a direct hit, the target must
    # arrive via the one-hop graph expansion, not as a direct hit.
    hits = memory.recall("semantic", "dinner main course", limit=1, graph=True)
    ids = [h.point.id for h in hits]
    assert source.id in ids and target.id in ids
    assert any(h.graph_expanded for h in hits if h.point.id == target.id)


def test_kb_import_duplicates_and_ingests_and_recalls(tmp_path):
    memory = _memory(tmp_path)
    kb = KnowledgeBase(memory)
    source = tmp_path / "note.txt"
    source.write_text("Roast beef is the main course.\n\nServe with potatoes.")
    target = kb.import_file(source)
    assert target.parent == kb.path
    assert target.exists()
    hits = kb.recall("roast beef main course")
    assert hits and "Roast beef" in hits[0].point.text
    # Scan again: unchanged file is not re-ingested as duplicates.
    before = len(memory.list_points("knowledge", limit=100))
    kb.scan()
    after = len(memory.list_points("knowledge", limit=100))
    assert after == before


def test_chunk_text_splits_paragraphs(tmp_path):
    # Small paragraphs combine into one chunk up to the size limit.
    chunks = chunk_text("First paragraph.\n\nSecond paragraph.")
    assert len(chunks) == 1
    assert "First paragraph." in chunks[0] and "Second paragraph." in chunks[0]
    # A long text splits into more than one chunk.
    long_text = ("Word " * 300) + "\n\n" + ("Other " * 300)
    assert len(chunk_text(long_text)) > 1


def test_experience_build_recall_and_loop_reuse(tmp_path):
    memory = _memory(tmp_path)
    experience = build_experience(
        "Write a note", "write a note",
        [{"tool": "write", "args": {"file": "deliverable.md"},
          "deliverable": "deliverable.md"}],
        ["write"], [], "done", "Write deliverable first.")
    store_experience(memory, experience)
    recalled = recall_experiences(memory, "Write a note")
    assert recalled and recalled[0].dag_nodes[0]["deliverable"] == "deliverable.md"

    # Loop: first run builds an Experience, second run reuses it.
    home = tmp_path / "home2"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, memory=memory)
    report1 = runner.run("Write a note")
    assert report1.status == "done"
    # Second run, same goal, with a fresh workdir, should reuse
    # the Experience DAG and still deliver the file.
    sandbox2 = Sandbox(home / "work2")
    runner2 = GoalRunner(FakeClient(""), sandbox2, gate, home, memory=memory)
    report2 = runner2.run("Write a note")
    assert report2.status == "done"
    assert report2.files == ["deliverable.md"]
