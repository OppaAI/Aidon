"""Semantic router vector cache, and image model .env wiring."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.system.config import Config
from src.memory.embedder import HashEmbedder
from src.agentic.semantic_router import SemanticRouter
from src.agentic.prompts import current_time_line, runtime_model_inventory


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


def test_natural_online_request_routes_to_webchat():
    router = SemanticRouter(HashEmbedder())
    goal = (
        "We are doing a demo video for a hackathon. Can you chat online "
        "to see if I qualify for the hackathon?"
    )
    assert router.classify(goal)[0] == "webchat"


def test_greeting_does_not_route_to_webchat():
    router = SemanticRouter(HashEmbedder())
    assert router.classify("Hey Aiden, how are you doing today?")[0] == "chat"


def test_qualification_and_rules_questions_route_to_webchat():
    router = SemanticRouter(HashEmbedder())
    assert router.classify("Am I qualified for the Meta Global AI Hackathon?")[0] == "webchat"
    assert router.classify("What are the rules for this hackathon?")[0] == "webchat"


def test_compliance_and_regulatory_questions_route_to_webchat():
    router = SemanticRouter(HashEmbedder())
    for question in (
        "Does this comply with the law?",
        "What regulations apply to this?",
        "Does this meet the required standard?",
        "Is this compliant with the regulations?",
    ):
        assert router.classify(question)[0] == "webchat"


def test_image_model_env_default_and_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDON_HOME", str(tmp_path))
    monkeypatch.delenv("AIDON_IMAGE_MODEL", raising=False)
    config = Config.load()
    assert config.image_model == "muse-image-1.0"
    monkeypatch.setenv("AIDON_IMAGE_MODEL", "muse-image-2.0")
    config2 = Config.load()
    assert config2.image_model == "muse-image-2.0"


def test_runtime_model_inventory_is_non_secret_and_configured(monkeypatch):
    monkeypatch.setenv("AIDON_MODEL", "muse-spark-1.3-contributor")
    monkeypatch.setenv("AIDON_BACKEND", "meta")
    monkeypatch.setenv("AIDON_FAST_MODEL", "muse-fast")
    monkeypatch.setenv("AIDON_API_KEY", "do-not-print-this")
    block = runtime_model_inventory()
    assert "muse-spark-1.3-contributor" in block
    assert "muse-fast" in block
    assert "do-not-print-this" not in block
    assert "muse-voice-transcribe-1.0" in block


def test_runtime_prompt_includes_local_time(monkeypatch):
    monkeypatch.setenv("AIDON_TIMEZONE", "America/Vancouver")
    line = current_time_line()
    assert "Current local time:" in line
    assert "America/Vancouver" in line
