"""Semantic router vector cache, and image model .env wiring."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from src.embedder import HashEmbedder
from src.semantic_router import SemanticRouter


class CountingEmbedder(HashEmbedder):
    def __init__(self):
        super().__init__()
        self.calls = 0
    def embed_batch(self, texts):
        self.calls += 1
        return super().embed_batch(texts)


def test_router_cache_writes_and_reuses(tmp_path):
    embedder = CountingEmbedder()
    cache = tmp_path / "semantic_router_vectors.json"
    router1 = SemanticRouter(embedder, cache_path=cache)
    assert cache.exists()
    calls_after_first = embedder.calls
    assert calls_after_first > 0
    # Second router, same embedder, must load from cache, no new embed calls.
    router2 = SemanticRouter(embedder, cache_path=cache)
    assert embedder.calls == calls_after_first
    # Both classify the same way, cache did not corrupt vectors.
    assert router1.classify("hello")[0] == router2.classify("hello")[0]


def test_router_cache_ignored_for_different_embedder(tmp_path):
    cache = tmp_path / "vectors.json"
    embedder1 = CountingEmbedder()
    SemanticRouter(embedder1, cache_path=cache)
    # A different embedder class name must not reuse the first cache.
    class OtherEmbedder(HashEmbedder):
        pass
    other = OtherEmbedder()
    router = SemanticRouter(other, cache_path=cache)
    assert router is not None
    # Cache file was overwritten with the new embedder name.
    data = json.loads(cache.read_text())
    assert data["embedder"] == "OtherEmbedder"


def test_image_model_env_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDON_HOME", str(tmp_path))
    monkeypatch.delenv("AIDON_IMAGE_MODEL", raising=False)
    config = Config.load()
    assert config.image_model == "muse-image-1.0"
    monkeypatch.setenv("AIDON_IMAGE_MODEL", "muse-image-2.0")
    config2 = Config.load()
    assert config2.image_model == "muse-image-2.0"
