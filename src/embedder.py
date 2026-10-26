"""Embedder interface: text in, vector out.

Qdrant stores vectors, it does not create them. The memory and
KB framework therefore takes an embedder as a parameter, and
never hard-codes one model.

- ``HashEmbedder`` is the offline, testable default. It hashes
  words into a fixed 128-dim vector and normalizes it. It is
  deterministic, it needs no download, and it proves the
  framework end to end. It is NOT semantic: two different words
  with the same hash collide, and related words are not close.
  Use it for tests and for framework proof only.
- A real embedder for production RAG plugs in here behind the
  same interface: a local embedding model, or a hosted
  embedding endpoint, if one is available. That swap changes
  the embedder, not the memory code, the collections, or the
  recall code.

Sparse recall, keyword tokens with weights, is separate from
dense vectors and is computed in memory.py from the text itself,
so keyword RAG works even with the hash embedder.
"""

from __future__ import annotations

import hashlib
import math
import re

DIM = 128
_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def sparse_weights(text: str) -> dict[str, float]:
    """Keyword token -> weight, for sparse / keyword recall."""
    weights: dict[str, float] = {}
    for token in tokens(text):
        weights[token] = weights.get(token, 0.0) + 1.0
    return weights


class Embedder:
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class HashEmbedder(Embedder):
    """Deterministic offline embedder, for tests and framework proof."""

    def __init__(self, dim: int = DIM):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokens(text):
            h = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FastEmbedEmbedder(Embedder):
    """Optional real embedder, only if fastembed is installed.

    Imported lazily, so the framework works without it. If it
    is not installed, construction raises a clear error telling
    the user to install it or to use HashEmbedder for tests.
    """

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed; install it for a real "
                "embedder, or use HashEmbedder for tests") from exc
        self._model = TextEmbedding(model)

    def embed(self, text: str) -> list[float]:
        vec = list(next(iter(self._model.embed([text]))))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]
