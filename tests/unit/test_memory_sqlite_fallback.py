"""The dependency-free memory fallback must survive a new process."""

from src.memory.embedder import HashEmbedder
from src.memory.memory import Memory, SQLiteStore


def test_splade_vectors_persist_in_sqlite(tmp_path):
    import json
    from types import SimpleNamespace

    import pytest

    np = pytest.importorskip("numpy")
    from src.memory.embedder import SpladeEmbedder

    sparse = SpladeEmbedder.__new__(SpladeEmbedder)
    sparse._sparse = SimpleNamespace(embed=lambda texts: iter([
        SimpleNamespace(indices=np.array([2, 9], dtype=np.int64),
                        values=np.array([0.5, 1.25], dtype=np.float32))]))
    vector = sparse.embed_sparse("hello")
    assert json.loads(json.dumps(vector)) == [[2, 9], [0.5, 1.25]]
    store = SQLiteStore(tmp_path)
    memory = Memory(tmp_path, embedder=HashEmbedder(), store=store)
    point = memory._record_with_vector(
        "working", "hello", np.array([0.5], dtype=np.float32),
        (np.array([2, 9]), np.array([0.5, 1.25], dtype=np.float32)), {})
    restored = SQLiteStore(tmp_path).get("working", point.id)
    assert restored.vector == [0.5]
    assert restored.sparse == ([2, 9], [0.5, 1.25])


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


def test_hybrid_search_ignores_mismatched_dimensions_for_dense_only():
    from src.memory.stores import Point, _hybrid_search

    hits = _hybrid_search([
        Point("wrong-dim", "query keyword", [1.0, 1.0, 1.0], sparse=([], [])),
        Point("right-dim", "unrelated text", [1.0, 0.0], sparse=([], [])),
    ], [1.0, 0.0], "query keyword", 2)
    ids = [hit.point.id for hit in hits]
    assert "wrong-dim" in ids  # retained through sparse text ranking
    assert ids.index("right-dim") < ids.index("wrong-dim")


def test_sqlite_faiss_search_refreshes_external_connection(tmp_path):
    import pytest

    pytest.importorskip("faiss")
    from src.memory.memory import Point, SQLiteFaissStore

    first = SQLiteFaissStore(tmp_path, dim=2)
    second = SQLiteFaissStore(tmp_path, dim=2)
    second.upsert("semantic", Point(
        "external", "fresh external point", [1.0, 0.0],
        sparse=([], []), payload={}))

    hits = first.search("semantic", [1.0, 0.0], "fresh external point", 5)
    assert any(hit.point.id == "external" for hit in hits)


def test_auto_backend_preserves_existing_local_qdrant(tmp_path, monkeypatch):
    import pytest
    import src.memory.stores as stores

    qdrant_home = tmp_path / "qdrant"
    qdrant_home.mkdir()
    (qdrant_home / "collection.data").write_text("existing", encoding="utf-8")
    monkeypatch.delenv("AIDON_MEMORY_BACKEND", raising=False)
    monkeypatch.delenv("AIDON_QDRANT_URL", raising=False)
    sentinel = object()
    monkeypatch.setattr(stores, "QdrantStore",
                        lambda *args, **kwargs: sentinel)
    monkeypatch.setattr(stores, "SQLiteFaissStore",
                        lambda *args, **kwargs: pytest.fail("should preserve Qdrant"))
    assert stores.make_store(tmp_path) is sentinel


def test_explicit_backend_failure_does_not_silently_fall_back(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import pytest
    import src.memory.embedder as embedder_module
    import src.memory.entity_graph as graph_module
    import src.memory.memory as memory_module
    from src.system.runtime import _make_memory

    monkeypatch.setenv("AIDON_MEMORY_BACKEND", "qdrant")
    monkeypatch.setattr(embedder_module, "make_embedder_cached",
                        lambda: HashEmbedder())
    monkeypatch.setattr(graph_module, "EntityGraph", lambda home: object())

    def fail_memory(*args, **kwargs):
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(memory_module, "Memory", fail_memory)
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        _make_memory(SimpleNamespace(home=tmp_path))
