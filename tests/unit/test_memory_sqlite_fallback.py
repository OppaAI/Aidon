"""The dependency-free memory fallback must survive a new process."""

from src.memory.embedder import HashEmbedder
from src.memory.memory import Memory, SQLiteStore


def test_sqlite_memory_persists_points(tmp_path):
    home = tmp_path / "aidon"
    first = Memory(home, embedder=HashEmbedder(), store=SQLiteStore(home))
    first.record_stm("user", "The hackathon rules matter", conversation_id="c1")
    episode = first.record_episodic("", "Hackathon rules", {"kind": "chat"})
    first.remember("The user is entering the Meta hackathon")

    second = Memory(home, embedder=HashEmbedder(), store=SQLiteStore(home))
    assert len(second.list_points("working", 20)) == 1
    assert len(second.list_points("episodic", 20)) == 1
    assert len(second.list_points("semantic", 20)) == 1
    assert second.store.get("episodic", episode.id).text == "Hackathon rules"
