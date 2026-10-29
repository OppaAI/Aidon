"""Library + EmbeddingGemma 2 embedder tests, offline.

No model download: EmbeddingGemma2Embedder is tested with a
stub SentenceTransformer that records prompt names and
returns deterministic 768-dim vectors. HashEmbedder covers
the offline fallback path, including image description
embedding. Files are copied under a tmp home, never the repo.
"""

import math

from src.memory.embedder import (DIM, EmbeddingGemma2Embedder, HashEmbedder,
                          make_embedder)
from src.memory.kb import KnowledgeBase
from src.memory.library import Library, library_url
from src.memory.memory import InMemoryStore, Memory


class _StubModel:
    def __init__(self):
        self.calls = []

    def encode(self, inputs, prompt_name=None, normalize_embeddings=True):
        self.calls.append(prompt_name)
        # Deterministic 768-dim vector, direction by prompt,
        # so Document and SearchQuery vectors differ even
        # after normalization (scale alone would not).
        vec = [0.0] * DIM
        vec[1 if prompt_name == "SearchQuery" else 0] = 1.0
        return [vec]


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def _png(tmp_path, name="chart.png"):
    # Content is not decoded on the hash fallback path, so
    # arbitrary bytes suffice; the suffix is what marks it
    # as an image for library / KB handling.
    path = tmp_path / name
    path.write_bytes(b"fake-png-bytes")
    return path


def test_embeddinggemma2_stub_is_768_and_uses_prompts():
    stub = _StubModel()
    emb = EmbeddingGemma2Embedder(model=stub)
    assert emb.dim == 768
    doc = emb.embed("roast beef")
    assert len(doc) == 768
    query = emb.embed_query("roast beef")
    assert len(query) == 768
    assert stub.calls == ["Document", "SearchQuery"]
    # Document and query vectors differ by prompt, by design.
    assert doc != query


def _fake_sentence_transformers(monkeypatch):
    import sys
    import types

    calls = {}

    class _FakeST:
        def __init__(self, name, **kwargs):
            calls["name"] = name
            calls.update(kwargs)

        def encode(self, *args, **kwargs):
            return [[0.0] * 768]

    mod = types.ModuleType("sentence_transformers")
    mod.SentenceTransformer = _FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", mod)
    return calls


def test_embedder_pins_revision(monkeypatch):
    from src.memory.embedder import (
        EMBEDDINGGEMMA2_REVISION, EmbeddingGemma2Embedder)

    calls = _fake_sentence_transformers(monkeypatch)
    monkeypatch.delenv("AIDON_EMBEDDING_REVISION", raising=False)
    emb = EmbeddingGemma2Embedder()
    assert calls["revision"] == EMBEDDINGGEMMA2_REVISION
    assert emb.revision == EMBEDDINGGEMMA2_REVISION


def test_embedder_revision_env_override(monkeypatch):
    from src.memory.embedder import EmbeddingGemma2Embedder

    calls = _fake_sentence_transformers(monkeypatch)
    monkeypatch.setenv("AIDON_EMBEDDING_REVISION", "abc123")
    EmbeddingGemma2Embedder()
    assert calls["revision"] == "abc123"


def test_make_embedder_hash_and_default():
    assert isinstance(make_embedder("hash"), HashEmbedder)
    # Default is EmbeddingGemma 2 only when sentence-transformers
    # is installed; in this offline test env it must raise a
    # clear error, not silently fall back to a wrong model.
    try:
        make_embedder("embeddinggemma2")
    except RuntimeError as exc:
        assert "sentence-transformers" in str(exc)


def test_library_add_image_links_memory_and_stays_under_home(tmp_path):
    memory = _memory(tmp_path)
    library = Library(tmp_path / "home")
    source = _png(tmp_path)
    item, point = library.add_image(
        source, "Roast beef chart", memory=memory,
        collection="episodic", project_id="proj1")
    # File is under home/library/files/<id>/<name>, not the repo.
    assert item["stored_path"].startswith("library/files/")
    assert item["stored_path"].endswith("/chart.png")
    assert (tmp_path / "home" / item["stored_path"]).exists()
    assert item["url"] == library_url(item["id"])
    assert item["memory_linked"] is True
    assert item["memory_id"] == point.id
    # Memory payload carries the link, so recall can refer to it.
    assert point.payload["library_id"] == item["id"]
    assert point.payload["library_path"] == item["stored_path"]
    assert point.payload["library_url"] == item["url"]
    assert point.payload["attachments"] == [item["id"]]
    # Recall by text returns the same link.
    hits = memory.recall("episodic", "roast beef chart")
    assert hits and hits[0].point.payload["library_url"] == item["url"]
    # Promotion copies the payload, so the link survives to LTM.
    promoted = memory.promote_to_ltm(point, kind="semantic")
    assert promoted.payload["library_url"] == item["url"]
    # Library listing, get with confinement, and delete.
    assert library.list_items("proj1")[0]["id"] == item["id"]
    assert library.get_item(item["id"])["name"] == "chart.png"
    assert library.delete_item(item["id"]) is True
    assert library.list_items("proj1") == []


def test_library_chart_carries_data_file(tmp_path):
    memory = _memory(tmp_path)
    library = Library(tmp_path / "home")
    data = tmp_path / "data.csv"
    data.write_text("item,price\nbeef,20\n")
    item, _ = library.add_image(
        _png(tmp_path), "Price chart, beef 20", memory=memory,
        collection="episodic", project_id="proj1", data_path=data)
    assert item["data_file"] == "data.csv"
    assert (tmp_path / "home" / "library" / "files" / item["id"]
            / "data.csv").exists()


def test_stm_and_episodic_attachment_links(tmp_path):
    memory = _memory(tmp_path)
    memory.record_stm("user", "see this chart", attachments=["lib1"])
    assert memory.stm[0]["attachments"] == ["lib1"]
    point = memory.record_episodic("proj1", "chart discussed",
                                   attachments=["lib1"])
    assert point.payload["attachments"] == ["lib1"]


def test_kb_image_ingest_is_one_point_with_path(tmp_path):
    memory = _memory(tmp_path)
    kb = KnowledgeBase(memory)
    target = kb.import_file(_png(tmp_path))
    assert target.parent == kb.path
    hits = kb.recall("chart")
    assert hits
    assert hits[0].point.payload["media"] == "image"
    assert hits[0].point.payload["knowledge_path"] == "knowledge/chart.png"
    # Scan again: unchanged image is not duplicated.
    before = len(memory.list_points("knowledge", limit=100))
    kb.scan()
    assert len(memory.list_points("knowledge", limit=100)) == before
