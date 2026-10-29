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


def test_sqlite_concurrent_writes_from_threads_do_not_lock(tmp_path):
    import threading

    from src.memory.memory import Point

    home = tmp_path / "aidon"
    store = SQLiteStore(home)
    errors = []

    def write(prefix):
        try:
            for i in range(20):
                store.upsert("working", Point(
                    id=f"{prefix}-{i}", text="t", vector=[0.1],
                    sparse=([], []), payload={}))
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f"t{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store._rows("working")) == 80
