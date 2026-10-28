"""Semantic router confidence: strong best matches are trusted."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.semantic_router import SemanticRouter


class StubEmbedder:
    """Returns one preset vector for any text, via embed_batch."""

    def __init__(self, vector):
        self.vector = vector

    def embed_batch(self, texts):
        return [list(self.vector) for _ in texts]


def _router(query_vector):
    vectors = {
        "chat": [[1.0, 0.0, 0.0, 0.0]],
        "webchat": [[0.98, 0.199, 0.0, 0.0]],
        "agentic": [[0.0, 1.0, 0.0, 0.0]],
        "artifact": [[0.0, 0.0, 1.0, 0.0]],
        "web_intent": [],
    }
    return SemanticRouter(StubEmbedder(query_vector), example_vectors=vectors)


def test_strong_best_match_is_trusted_despite_small_margin():
    # Chat 1.00 vs webchat 0.98: margin 0.02 used to give confidence
    # 0.10 and force agentic. The best match is strong, so chat wins.
    route, confidence = _router([1.0, 0.0, 0.0, 0.0]).classify("hi")
    assert route == "chat"
    assert confidence >= 0.5


def test_weak_best_match_still_fails_safe_to_agentic():
    # Nothing matches well: the highest similarity is low, so the
    # safe agentic fallback still applies.
    route, confidence = _router([0.0, 0.0, 0.0, 1.0]).classify("???")
    assert route == "agentic"
    assert confidence < 0.15
