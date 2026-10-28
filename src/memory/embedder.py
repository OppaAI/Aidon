"""Embedder interface: text and images in, vectors out.

Qdrant stores vectors, it does not create them. The memory and
KB framework therefore takes an embedder as a parameter, and
never hard-codes one model.

Production embedder, by user decision:
- ``EmbeddingGemma2Embedder``: ``google/embeddinggemma-2`` via
  sentence-transformers (>= 6.1.0). Loaded with
  ``config_kwargs={"audio_config": None}``, which is the 440M
  configuration: 270M text backbone + 170M vision encoder,
  audio omitted. Output is 768-dim, in one shared space for
  text and images, so a text query can recall an image and
  vice versa. Text-only is 270M (both vision and audio None),
  full multimodal is 740M, but this repo keeps 440M because
  image input and output are part of memory, audio is not.
  Precision: bfloat16 or float32, never float16, which
  EmbeddingGemma 2 activations do not tolerate.
  Task prompts, per the model guide: documents use
  ``Document``, queries use ``SearchQuery``.
  The model is Apache 2.0, ungated, and it runs locally, so
  embeddings are computed on the user's own machine, not sent
  to a central service. ``HF_HUB_OFFLINE=1`` is respected by
  the underlying hub loader for a cached model.

Test / offline embedder:
- ``HashEmbedder`` is the offline, testable default. It hashes
  words into a fixed 128-dim vector and normalizes it. It is
  deterministic, it needs no download, and it proves the
  framework end to end. It is NOT semantic: two different
  words with the same hash collide, and related words are not
  close. Use it for tests and for framework proof only.
  Its ``embed_image`` falls back to embedding the image
  description text, because a hash embedder has no vision
  encoder. A real image vector requires the 440M embedder.

Sparse recall, keyword tokens with weights, is separate from
dense vectors and is computed in memory.py from the text itself,
so keyword RAG works even with the hash embedder.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import threading

DIM = 768
HASH_DIM = 128
EMBEDDINGGEMMA2_MODEL = "google/embeddinggemma-2"
DEFAULT_SPARSE_MODEL = "prithivida/Splade_PP_en_v1"
_WORD = re.compile(r"[a-z0-9]+")


def tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def sparse_weights(text: str) -> dict[str, float]:
    """Keyword token -> weight, for sparse / keyword recall."""
    weights: dict[str, float] = {}
    for token in tokens(text):
        weights[token] = weights.get(token, 0.0) + 1.0
    return weights


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Embedder:
    dim: int = HASH_DIM

    def embed(self, text: str) -> list[float]:
        raise NotImplementedError

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        """Query-side embedding. Default: same as a document."""
        return self.embed(text)

    def embed_image(self, path, description: str = "") -> list[float]:
        """Image-side embedding. Default: embed the description.

        A real vision embedder overrides this with an image
        vector. The fallback keeps the framework working offline:
        an image is still retrievable by its description text,
        but it is not a vision match.
        """
        return self.embed(description or str(path))

    def embed_sparse(self, text: str) -> tuple[list[int], list[float]]:
        """Sparse vector for hybrid recall.

        Default, offline: keyword tokens, hashed to a stable
        index space, weight = count. This is a BM25-like
        keyword side, not SPLADE, but it gives the same
        shape: dense KNN + sparse keyword, fused with RRF.
        A real SPLADE embedder overrides this with learned
        sparse weights, see SpladeEmbedder and HybridEmbedder.
        """
        weights = sparse_weights(text)
        items = sorted(
            (int(hashlib.sha256(t.encode()).hexdigest()[:8], 16) % 100000,
             w) for t, w in weights.items())
        return ([i for i, _ in items], [w for _, w in items])


class HashEmbedder(Embedder):
    """Deterministic offline embedder, for tests and framework proof."""

    def __init__(self, dim: int = HASH_DIM):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in tokens(text):
            h = int(hashlib.sha256(token.encode()).hexdigest()[:8], 16)
            vec[h % self.dim] += 1.0
        return _normalize(vec)


class EmbeddingGemma2Embedder(Embedder):
    """EmbeddingGemma 2, 440M text + vision, audio omitted.

    ``model`` is injected for tests: a stub with an ``encode``
    method can stand in for SentenceTransformer, so the image
    and prompt paths are testable without downloading 440M.
    In production, ``model`` is None and SentenceTransformer
    is loaded lazily, so importing this module never downloads
    anything.
    """

    dim = DIM

    def __init__(self, model_name: str = EMBEDDINGGEMMA2_MODEL,
                 model=None):
        self.model_name = model_name
        if model is not None:
            self._model = model
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed; install it "
                "for the real embedder (pip install -U "
                "'sentence-transformers[image]>=6.1' transformers "
                "Pillow), or use HashEmbedder for tests") from exc
        # 440M: text + vision, audio omitted. Text-only would
        # also set vision_config=None, full would set neither.
        self._model = SentenceTransformer(
            model_name, config_kwargs={"audio_config": None})

    def _encode(self, inputs, prompt_name: str | None = None):
        try:
            if prompt_name is not None:
                raw = self._model.encode(
                    inputs, prompt_name=prompt_name,
                    normalize_embeddings=True)
            else:
                raw = self._model.encode(
                    inputs, normalize_embeddings=True)
        except TypeError:
            # Stub models and older wrappers without prompt_name.
            raw = self._model.encode(inputs, normalize_embeddings=True)
        # encode() returns a batch: numpy (1, 768) or [[...]].
        # A single-item wrapper may return a flat vector instead.
        if hasattr(raw, "tolist"):
            raw = raw.tolist()
        if isinstance(raw, (list, tuple)) and raw and \
                isinstance(raw[0], (list, tuple)):
            vec = list(raw[0])
        else:
            vec = list(raw)
        return _normalize([float(v) for v in vec])

    def embed(self, text: str) -> list[float]:
        return self._encode([text], prompt_name="Document")

    def embed_query(self, text: str) -> list[float]:
        return self._encode([text], prompt_name="SearchQuery")

    def embed_image(self, path, description: str = "") -> list[float]:
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "Pillow is not installed; install it for image "
                "embedding, or use a text description") from exc
        with Image.open(path) as image:
            image = image.convert("RGB")
            return self._encode([image])


class SpladeEmbedder:
    """Learned sparse side, prithivida/Splade_PP_en_v1 via FastEmbed.

    Only the sparse side: dense comes from EmbeddingGemma 2.
    If fastembed is not installed, construction raises a clear
    error, and the caller falls back to keyword sparse.
    """

    def __init__(self, model_name: str = "prithivida/Splade_PP_en_v1"):
        try:
            from fastembed import SparseTextEmbedding
        except ImportError as exc:
            raise RuntimeError(
                "fastembed is not installed; install it for SPLADE "
                "sparse, or use the keyword sparse fallback") from exc
        self._sparse = SparseTextEmbedding(model_name)

    def embed_sparse(self, text: str) -> tuple[list[int], list[float]]:
        raw = next(iter(self._sparse.embed([text])))
        return (list(raw.indices), list(raw.values))


class HybridEmbedder(Embedder):
    """Dense + learned sparse, in one embedder interface.

    Dense: any Embedder, normally EmbeddingGemma 2 440M.
    Sparse: any object with embed_sparse(), normally
    SpladeEmbedder, or the keyword fallback on the dense
    embedder itself. Dim is the dense dim.
    """

    def __init__(self, dense: Embedder, sparse=None):
        self._dense = dense
        self._sparse = sparse or dense
        self.dim = getattr(dense, "dim", HASH_DIM)

    def embed(self, text: str) -> list[float]:
        return self._dense.embed(text)

    def embed_query(self, text: str) -> list[float]:
        return self._dense.embed_query(text)

    def embed_image(self, path, description: str = "") -> list[float]:
        return self._dense.embed_image(path, description)

    def embed_sparse(self, text: str) -> tuple[list[int], list[float]]:
        return self._sparse.embed_sparse(text)


def make_embedder(name: str | None = None) -> Embedder:
    """Factory from env / name.

    ``hash`` -> HashEmbedder (tests, offline).
    Anything else, or empty -> EmbeddingGemma 2 440M, with
    AIDON_EMBED_MODEL overriding the model id, and
    AIDON_SPARSE_MODEL, when set, wrapping it in a HybridEmbedder
    with SPLADE sparse. The default is the Aidon SPLADE model;
    set AIDON_SPARSE_MODEL= (an explicitly empty value) to opt out
    and use the deterministic keyword sparse fallback instead.
    """
    name = (name or os.environ.get("AIDON_EMBEDDER", "")).strip()
    if name == "hash":
        return HashEmbedder()
    model_name = os.environ.get("AIDON_EMBED_MODEL",
                                EMBEDDINGGEMMA2_MODEL).strip()
    dense = EmbeddingGemma2Embedder(model_name=model_name or
                                    EMBEDDINGGEMMA2_MODEL)
    sparse_model = os.environ.get("AIDON_SPARSE_MODEL",
                                  DEFAULT_SPARSE_MODEL).strip()
    if sparse_model:
        return HybridEmbedder(dense, SpladeEmbedder(sparse_model))
    return dense


_EMBEDDER_CACHE: dict[str, Embedder] = {}
_EMBEDDER_LOCK = threading.Lock()


def make_embedder_cached(name: str | None = None) -> Embedder:
    """One embedder per model name per process, shared.

    Loading EmbeddingGemma 2 takes seconds and hundreds of
    megabytes. Creating a new embedder per Memory, per goal,
    or per run would pay that cost repeatedly. The cache
    keeps one instance per (name, model, sparse) triple.
    Hash embedders are cheap and are not cached, so tests
    stay isolated.
    """
    key = (name or os.environ.get("AIDON_EMBEDDER", ""),
           os.environ.get("AIDON_EMBED_MODEL", ""),
           os.environ.get("AIDON_SPARSE_MODEL", ""))
    if key[0] == "hash":
        return HashEmbedder()
    with _EMBEDDER_LOCK:
        cached = _EMBEDDER_CACHE.get(str(key))
        if cached is None:
            cached = make_embedder(name)
            _EMBEDDER_CACHE[str(key)] = cached
        return cached
