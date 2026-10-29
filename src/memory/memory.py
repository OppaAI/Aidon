"""Memory and knowledge framework, Qdrant preferred with SQLite fallback.

One Qdrant instance is used in normal installations, local mode under
the Aidon home by default or a server URL if AIDON_QDRANT_URL is set.
If qdrant-client is not installed, the same collections use the
dependency-free SQLiteStore so memory remains durable instead of being
silently discarded at process exit.

Taxonomy, as defined for Aidon:
- Working memory (STM): chat history. Recent exchanges, kept
  in memory for context (last 20 by default) and recorded to
  the ``working`` collection for session recall. STM is never
  promoted to long-term memory on its own.
- Episodic memory: project-specific memory. Interview
  questions and answers, and specific information and data
  within project tasks, scoped by project_id, in the
  ``episodic`` collection. This is what a project remembers
  about itself, not what Aidon knows in general.
- Semantic memory (LTM, global): facts, in the ``semantic``
  collection. Not project-scoped, reusable across projects.
- Procedural memory (LTM, global): skills, tools, and
  workflows, in the ``experience`` collection. See
  experience.py: an Experience is a successful run's workflow
  with its DAG nodes, tools, and skills, unified with
  procedural memory. It is not a separate store.
- Knowledge (KB, RAG): ingested files, in the ``knowledge``
  collection. See kb.py: files in knowledge/ are chunked and
  ingested here for retrieval.

Recall, hybrid, same shape as the earlier design, better
where it is cheap to be better:
- Dense vector (EmbeddingGemma 2, 768-dim) plus a sparse
  vector (SPLADE when configured, otherwise keyword tokens
  hashed to a stable index space). Qdrant stores both as
  named vectors, ``dense`` and ``sparse``, and recall is
  one hybrid query: dense KNN prefetch plus sparse
  prefetch, fused with RRF, over-fetching 2x. The in-memory
  fallback does the same RRF fusion over dense and sparse
  rankings, so tests and the real store agree on shape.
- An optional one-hop knowledge-graph expansion: a point
  whose payload has ``links`` pulls those points in as
  graph-expanded results, within the same collection.
- A recall is an access: access_count, last_accessed, and
  access_day_count are updated, best-effort, so the
  forgetting curve below has real data to work on.

Forgetting curve, deterministic, no model, no I/O:
  strength = 1 + log1p(min(access_count, 255))
  half_life_effective = half_life_days * (1 + 0.35 * log1p(access_day_count))
  score = strength * 0.5 ** (days_since_last_access / half_life_effective)
Frequent recall helps retention without letting raw counts
dominate, and spaced repetition on distinct days stretches
the half-life. New points get a grace window (35 days by
default) during which they are never prune candidates.
Prune is always explicit: ``prune()`` returns what it
removed, it never runs on its own, and it never deletes
promoted LTM or library files, only memory points.

Promotion, episodic -> LTM:
At the end of a completed run, Aidon may ask the user whether
to put that run's episodic memories into LTM. If the
environment flag AIDON_AUTO_PROMOTE_TO_LTM is "1", promotion
happens automatically, without asking. Otherwise the default
is to ask, via a caller-supplied ask callback, and a "no"
leaves episodic memories project-scoped only. Promotion
copies a point to the semantic collection (for a fact) or
to the experience collection (for a workflow), and marks
the episodic point as promoted, it does not delete the
episodic original, and it copies the payload, so a library
link survives promotion.

Projects and conversations, without a second store:
Project and conversation ids are payload fields on working
and episodic points. ``conversation_history`` and
``list_projects`` read them back from Qdrant by filtered
scroll, in created order, so a conversation survives a
restart without SQLite. ``conversation_context`` returns
the last exchanges for a conversation, plus a stored
summary point if one exists; model-written summaries come
with the interview part, this framework stores and returns
them, it does not invent them.

Images: see library.py. A memory point never holds image
bytes; it holds the image vector and the library link, so
recall returns the file reference alongside the text.

If qdrant_client is not installed, the store falls back to
an in-memory store with the same collection / point / search
shape, so tests run offline. When qdrant_client is installed,
the real Qdrant store is used, local mode by default, with
one shared client per path or URL per process.
"""

from __future__ import annotations

import atexit
import math
import json
import sqlite3
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .embedder import Embedder, HashEmbedder, sparse_weights, tokens

COLLECTIONS = ("working", "episodic", "semantic", "experience",
               "knowledge")
_RRF_K = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_iso(value: str | None):
    if not value or value == "never":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def decay_score(access_count: int, last_accessed_iso: str | None, *,
                half_life_days: float = 21.0,
                access_day_count: int = 0) -> float:
    """Ebbinghaus-style decay score. Higher = more alive."""
    try:
        count = max(0, int(access_count or 0))
    except (TypeError, ValueError):
        return 0.0
    if count <= 0:
        return 0.0
    last = _parse_iso(last_accessed_iso)
    if last is None:
        return 0.0
    days = max(0.0, (datetime.now(timezone.utc) - last).total_seconds() / 86400.0)
    strength = 1.0 + math.log1p(min(count, 255))
    try:
        day_n = max(0, int(access_day_count or 0))
    except (TypeError, ValueError):
        day_n = 0
    effective = max(float(half_life_days), 1e-6) * (1.0 + 0.35 * math.log1p(day_n))
    return float(strength) * (0.5 ** (days / effective))


def is_grace_protected(created_at_iso: str | None, grace_days: int = 35) -> bool:
    created = _parse_iso(created_at_iso)
    if created is None:
        return False
    days = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
    return days < grace_days


def should_prune(payload: dict, *, half_life_days: float = 21.0,
                 threshold: float = 0.02, grace_days: int = 35) -> bool:
    """True when a point is a cleanup candidate, never for grace."""
    if is_grace_protected(payload.get("created_at"), grace_days):
        return False
    return decay_score(payload.get("access_count", 0),
                       payload.get("last_accessed"),
                       half_life_days=half_life_days,
                       access_day_count=payload.get("access_day_count", 0)) < threshold


@dataclass
class Point:
    id: str
    text: str
    vector: list[float]
    payload: dict = field(default_factory=dict)
    sparse: tuple[list[int], list[float]] = field(
        default_factory=lambda: ([], []))


@dataclass
class Hit:
    point: Point
    score: float
    graph_expanded: bool = False


def retention_rank(hits, limit: int):
    """Re-rank recall hits by vector score + extraction importance.

    Extracted facts/understandings carry an importance payload from
    the strong-model extraction pass. A small bonus (0.25 x
    importance) lifts important items above equally relevant ones
    without ever outranking a clearly more relevant vector hit —
    the same restrained-bonus spirit as the halved graph-expansion
    score. Plain points carry no importance and keep their vector
    score. Returns at most limit hits, best first.
    """
    def key(hit):
        payload = getattr(hit.point, "payload", {}) or {}
        bonus = 0.0
        if payload.get("kind") in ("fact", "understanding"):
            try:
                bonus = 0.25 * float(payload.get("importance", 0.5))
            except (TypeError, ValueError):
                bonus = 0.0
        return hit.score + bonus

    return sorted(hits, key=key, reverse=True)[:max(0, limit)]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _rrf(dense_ranked: list[Point], sparse_ranked: list[Point],
         limit: int) -> list[Hit]:
    """Reciprocal rank fusion over two rankings, k=60."""
    scores: dict[str, float] = {}
    points: dict[str, Point] = {}
    for rank, point in enumerate(dense_ranked):
        scores[point.id] = scores.get(point.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        points[point.id] = point
    for rank, point in enumerate(sparse_ranked):
        scores[point.id] = scores.get(point.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        points[point.id] = point
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [Hit(points[pid], score) for pid, score in ordered[:limit]]


class InMemoryStore:
    """Qdrant-shaped in-memory store, for tests and fallback."""

    def __init__(self):
        self._data: dict[str, dict[str, Point]] = {
            name: {} for name in COLLECTIONS}

    def upsert(self, collection: str, point: Point) -> None:
        self._data[collection][point.id] = point

    def get(self, collection: str, point_id: str) -> Point | None:
        return self._data[collection].get(point_id)

    def delete(self, collection: str, point_id: str) -> bool:
        return self._data[collection].pop(point_id, None) is not None

    def set_payload(self, collection: str, point_id: str,
                    payload: dict) -> None:
        point = self._data[collection].get(point_id)
        if point is not None:
            point.payload.update(payload)

    def delete_by_project(self, collection: str, project_id: str) -> int:
        return self.delete_where(collection, {"project_id": project_id})

    def delete_where(self, collection: str, filters: dict) -> int:
        doomed = [pid for pid, p in self._data[collection].items()
                  if all(p.payload.get(k) == v for k, v in filters.items())]
        for pid in doomed:
            del self._data[collection][pid]
        return len(doomed)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return list(self._data[collection].values())[:limit]

    def iter_points(self, collection: str):
        """Every point, not just the first page, for prune and export."""
        yield from list(self._data[collection].values())

    def list_filtered(self, collection: str, filters: dict,
                      limit: int = 1000) -> list[Point]:
        out = []
        for point in self._data[collection].values():
            if all(point.payload.get(k) == v for k, v in filters.items()):
                out.append(point)
        # Created order, oldest first, for conversation history.
        out.sort(key=lambda p: p.payload.get("created_at", ""))
        return out[:limit]

    def search(self, collection: str, vector: list[float],
               query_text: str, limit: int = 5,
               sparse: tuple[list[int], list[float]] | None = None) -> list[Hit]:
        if limit <= 0:
            return []
        points = list(self._data[collection].values())
        dense_ranked = sorted(
            points, key=lambda p: _cosine(vector, p.vector), reverse=True)
        query_sparse = dict(zip(sparse[0], sparse[1])) if sparse else {}
        if not query_sparse:
            query_sparse = dict(zip(*_sparse_from_text(query_text)))
        def sparse_score(point: Point) -> float:
            point_sparse = dict(zip(point.sparse[0], point.sparse[1])) \
                if point.sparse and point.sparse[0] else \
                dict(zip(*_sparse_from_text(point.text)))
            return sum(min(query_sparse.get(i, 0.0), w)
                       for i, w in point_sparse.items())
        sparse_ranked = sorted(points, key=sparse_score, reverse=True)
        over = max(limit * 2, 4)
        return _rrf(dense_ranked[:over], sparse_ranked[:over], limit)


class SQLiteStore(InMemoryStore):
    """Dependency-free persistent fallback for the memory collections.

    Qdrant remains the preferred vector store when installed.  This
    fallback keeps memory durable on a minimal installation, rather than
    silently losing every point when the process exits.  Search uses the
    same dense+sparse ranking as ``InMemoryStore``; it is intentionally a
    reliability fallback, not a replacement for Qdrant at scale.
    """

    def __init__(self, home: Path):
        self._db_path = Path(home) / "memory.sqlite3"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=30.0)
        self._lock = threading.RLock()
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS memory_points ("
            "collection TEXT NOT NULL, id TEXT NOT NULL, text TEXT NOT NULL, "
            "vector TEXT NOT NULL, sparse TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY(collection, id))")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_created "
            "ON memory_points(collection, id)")
        self._db.commit()

    @staticmethod
    def _point(row) -> Point:
        return Point(
            id=str(row["id"]), text=str(row["text"]),
            vector=list(json.loads(row["vector"])),
            sparse=tuple(json.loads(row["sparse"])),
            payload=dict(json.loads(row["payload"])),
        )

    def upsert(self, collection: str, point: Point) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO memory_points(collection,id,text,vector,sparse,payload) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(collection,id) DO UPDATE SET "
                "text=excluded.text, vector=excluded.vector, sparse=excluded.sparse, "
                "payload=excluded.payload",
                (collection, point.id, point.text, json.dumps(point.vector),
                 json.dumps([list(point.sparse[0]), list(point.sparse[1])]),
                 json.dumps(point.payload)),
            )
            self._db.commit()

    def get(self, collection: str, point_id: str) -> Point | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM memory_points WHERE collection=? AND id=?",
                (collection, str(point_id))).fetchone()
            return self._point(row) if row else None

    def delete(self, collection: str, point_id: str) -> bool:
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM memory_points WHERE collection=? AND id=?",
                (collection, str(point_id)))
            self._db.commit()
            return cur.rowcount > 0

    def set_payload(self, collection: str, point_id: str,
                    payload: dict) -> None:
        point = self.get(collection, point_id)
        if point is None:
            return
        point.payload.update(payload)
        self.upsert(collection, point)

    def _rows(self, collection: str, limit: int = 100000,
              filters: dict | None = None):
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM memory_points WHERE collection=?",
                (collection,)).fetchall()
            points = [self._point(row) for row in rows]
            if filters:
                points = [p for p in points if all(
                    p.payload.get(k) == v for k, v in filters.items())]
            points.sort(key=lambda p: p.payload.get("created_at", ""))
            return points[:limit]

    def delete_where(self, collection: str, filters: dict) -> int:
        doomed = [p.id for p in self._rows(collection) if all(
            p.payload.get(k) == v for k, v in filters.items())]
        for point_id in doomed:
            self.delete(collection, point_id)
        return len(doomed)

    def delete_by_project(self, collection: str, project_id: str) -> int:
        return self.delete_where(collection, {"project_id": project_id})

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return self._rows(collection, limit)

    def iter_points(self, collection: str):
        yield from self._rows(collection)

    def list_filtered(self, collection: str, filters: dict,
                      limit: int = 1000) -> list[Point]:
        return self._rows(collection, limit, filters)

    def search(self, collection: str, vector: list[float], query_text: str,
               limit: int = 5,
               sparse: tuple[list[int], list[float]] | None = None) -> list[Hit]:
        # Do NOT delegate to InMemoryStore.search: it ranks over
        # self._data, which SQLiteStore never builds (its __init__
        # opens SQLite instead). Rank over this store's own rows
        # with the same dense+sparse + RRF algorithm.
        if limit <= 0:
            return []
        points = self._rows(collection)
        dense_ranked = sorted(
            points, key=lambda p: _cosine(vector, p.vector), reverse=True)
        query_sparse = dict(zip(sparse[0], sparse[1])) if sparse else {}
        if not query_sparse:
            query_sparse = dict(zip(*_sparse_from_text(query_text)))

        def sparse_score(point: Point) -> float:
            point_sparse = dict(zip(point.sparse[0], point.sparse[1])) \
                if point.sparse and point.sparse[0] else \
                dict(zip(*_sparse_from_text(point.text)))
            return sum(min(query_sparse.get(i, 0.0), w)
                       for i, w in point_sparse.items())

        sparse_ranked = sorted(points, key=sparse_score, reverse=True)
        over = max(limit * 2, 4)
        return _rrf(dense_ranked[:over], sparse_ranked[:over], limit)


def _sparse_from_text(text: str):
    # Keyword sparse, via the base embedder helper, without
    # constructing an embedder instance.
    weights = sparse_weights(text)
    import hashlib
    items = sorted(
        (int(hashlib.sha256(t.encode()).hexdigest()[:8], 16) % 100000, w)
        for t, w in weights.items())
    return ([i for i, _ in items], [w for _, w in items])


_CLIENTS: dict[str, object] = {}
_CLIENTS_LOCK = threading.Lock()


def _close_clients() -> None:
    """Close shared Qdrant clients before interpreter shutdown.

    qdrant-client's local mode imports ``portalocker`` lazily inside
    ``close()``. When ``close()`` first runs from ``__del__`` during
    shutdown, ``sys.meta_path`` is already gone and Python prints an
    ImportError traceback on every command that touched memory. Closing
    once here, while imports still work, makes the later ``__del__`` a
    no-op (the lock file is already closed) and keeps exit quiet.
    """
    with _CLIENTS_LOCK:
        clients = list(_CLIENTS.values())
        _CLIENTS.clear()
    for client in clients:
        try:
            client.close()
        except Exception:
            pass


atexit.register(_close_clients)


def _shared_client(url: str, path: str):
    """One Qdrant client per path or URL per process.

    Local mode allows exactly one client per store path per
    process; overlapping goals in one process must share it,
    not crash on a second open. :memory: stores are never
    shared, or test isolation breaks.
    """
    from qdrant_client import QdrantClient
    if url:
        key = f"url:{url}"
        with _CLIENTS_LOCK:
            client = _CLIENTS.get(key)
            if client is None:
                client = QdrantClient(url=url)
                _CLIENTS[key] = client
            return client
    if path == ":memory:":
        from qdrant_client import QdrantClient
        return QdrantClient(path=path)
    key = f"path:{path}"
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(key)
        inner = getattr(client, "_client", None) if client else None
        if client is None or getattr(inner, "closed", False):
            from qdrant_client import QdrantClient
            client = QdrantClient(path=path)
            _CLIENTS[key] = client
        return client


class QdrantStore:
    """Real Qdrant store, local mode by default, or server URL.

    Collections use named vectors: ``dense`` (768 for
    EmbeddingGemma 2, 128 for HashEmbedder) and ``sparse``
    (SPLADE or keyword). A collection created with a
    different dense size cannot hold the new vectors; a
    new home, or a recreated collection, is the migration,
    never a silent resize.
    """

    def __init__(self, home: Path, url: str = "", dim: int = 768):
        from qdrant_client import models
        self._models = models
        self._client = _shared_client(url, str(Path(home) / "qdrant") if not url else "")
        self._embedder_dim = dim
        for name in COLLECTIONS:
            if not self._client.collection_exists(name):
                # Sparse vectors are NOT dense vectors: they belong in
                # sparse_vectors_config, not vectors_config. Putting a
                # SparseVectorParams in vectors_config makes newer
                # qdrant-client local mode fail with
                # AttributeError: 'SparseVectorParams' object has no
                # attribute 'size', because it validates every dense
                # vector size. This is the bug seen on a PC with a
                # newer qdrant-client, where Megumi's VM fell back
                # to InMemoryStore and never hit it.
                try:
                    self._client.create_collection(
                        name,
                        vectors_config={
                            "dense": models.VectorParams(
                                size=self._embedder_dim,
                                distance=models.Distance.COSINE),
                        },
                        sparse_vectors_config={
                            "sparse": models.SparseVectorParams(),
                        })
                except TypeError:
                    # Very old qdrant-client without sparse_vectors_config:
                    # fall back to the old form, which that client accepted.
                    self._client.create_collection(
                        name, vectors_config={
                            "dense": models.VectorParams(
                                size=self._embedder_dim,
                                distance=models.Distance.COSINE),
                            "sparse": models.SparseVectorParams(),
                        })

    def _to_point(self, raw) -> Point:
        payload = dict(raw.payload or {})
        text = payload.get("text", "")
        vector = []
        sparse: tuple[list[int], list[float]] = ([], [])
        if isinstance(raw.vector, dict):
            vector = list(raw.vector.get("dense") or [])
            # Keep the sparse vector. Dropping it here meant the next
            # upsert of this point (record_access does one on every
            # recall) rewrote it empty, so hybrid recall quietly
            # degraded to dense-only for every point that was ever
            # recalled.
            raw_sparse = raw.vector.get("sparse")
            if raw_sparse is not None:
                sparse = (list(getattr(raw_sparse, "indices", []) or []),
                          list(getattr(raw_sparse, "values", []) or []))
        elif raw.vector is not None:
            vector = list(raw.vector)
        return Point(id=str(raw.id), text=text, vector=vector,
                     payload=payload, sparse=sparse)

    def _vectors(self, point: Point):
        idx, val = point.sparse if point.sparse else ([], [])
        return {
            "dense": point.vector,
            "sparse": self._models.SparseVector(indices=idx, values=val),
        }

    def upsert(self, collection: str, point: Point) -> None:
        self._client.upsert(collection, points=[
            self._models.PointStruct(
                id=point.id, vector=self._vectors(point),
                payload={**point.payload, "text": point.text,
                         "tokens": sparse_weights(point.text)})])

    def get(self, collection: str, point_id: str) -> Point | None:
        try:
            raw = self._client.retrieve(collection, ids=[point_id],
                                        with_vectors=True)
        except Exception:
            return None
        return self._to_point(raw[0]) if raw else None

    def delete(self, collection: str, point_id: str) -> bool:
        try:
            self._client.delete(collection, points_selector=[point_id])
            return True
        except Exception:
            return False

    def set_payload(self, collection: str, point_id: str,
                    payload: dict) -> None:
        """Update payload only, vectors untouched."""
        self._client.set_payload(collection_name=collection,
                                 payload=payload, points=[point_id])

    def _scroll_filtered(self, collection: str, filters: dict, limit: int):
        conditions = [self._models.FieldCondition(
            key=k, match=self._models.MatchValue(value=v))
            for k, v in filters.items()]
        points, _ = self._client.scroll(
            collection,
            scroll_filter=self._models.Filter(must=conditions) if conditions else None,
            limit=limit, with_vectors=True)
        return [self._to_point(p) for p in points]

    def delete_by_project(self, collection: str, project_id: str) -> int:
        return self.delete_where(collection, {"project_id": project_id})

    def delete_where(self, collection: str, filters: dict) -> int:
        """Delete every point matching all filters, not just the first page."""
        conditions = [self._models.FieldCondition(
            key=k, match=self._models.MatchValue(value=v))
            for k, v in filters.items()]
        query_filter = self._models.Filter(must=conditions)
        try:
            count = self._client.count(
                collection_name=collection,
                count_filter=query_filter, exact=True).count
        except Exception:
            count = 0
        self._client.delete(
            collection_name=collection,
            points_selector=self._models.FilterSelector(
                filter=query_filter))
        return int(count)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        points, _ = self._client.scroll(collection, limit=limit, with_vectors=True)
        return [self._to_point(p) for p in points]

    def iter_points(self, collection: str):
        """Every point, paged with scroll, for prune and export.

        ``list_points`` is capped by its limit, so using it for prune
        or project discovery silently skipped everything past the cap.
        """
        offset = None
        while True:
            points, offset = self._client.scroll(
                collection, limit=256, offset=offset, with_vectors=True)
            for point in points:
                yield self._to_point(point)
            if offset is None:
                break

    def list_filtered(self, collection: str, filters: dict, limit: int = 1000) -> list[Point]:
        points = self._scroll_filtered(collection, filters, limit)
        points.sort(key=lambda p: p.payload.get("created_at", ""))
        return points

    def search(self, collection: str, vector: list[float],
               query_text: str, limit: int = 5,
               sparse: tuple[list[int], list[float]] | None = None) -> list[Hit]:
        if limit <= 0:
            return []
        over = max(limit * 2, 4)
        idx, val = sparse if sparse else _sparse_from_text(query_text)
        raw = self._client.query_points(
            collection,
            prefetch=[
                self._models.Prefetch(query=vector, using="dense", limit=over),
                self._models.Prefetch(
                    query=self._models.SparseVector(indices=idx, values=val),
                    using="sparse", limit=over),
            ],
            query=self._models.FusionQuery(fusion=self._models.Fusion.RRF),
            limit=limit, with_payload=True, with_vectors=True).points
        return [Hit(self._to_point(p), float(p.score or 0.0)) for p in raw]


def make_store(home: Path, dim: int = 768):
    """Real Qdrant if installed, otherwise a durable SQLite fallback."""
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        return SQLiteStore(home)
    url = os.environ.get("AIDON_QDRANT_URL", "").strip()
    return QdrantStore(home, url=url, dim=dim)


# Promotion supersede bar: at least half the shape tokens,
# the same bar experience merging uses for "the same thing
# in new words".
_PROMOTE_SUPERSEDE_SHAPE = 0.5

_PROMOTE_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "to", "of", "for", "in", "on",
    "with", "is", "it", "be", "i", "me", "my",
})


def _text_shape(text: str) -> set[str]:
    return {t for t in tokens(str(text or "")) if t not in _PROMOTE_STOPWORDS}


def _shape_overlap(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class Memory:
    """STM + episodic + semantic + experience + knowledge, Qdrant for all."""

    def __init__(self, home: Path, embedder: Embedder | None = None,
                 store=None, entity_graph=None):
        self.home = Path(home)
        self.entity_graph = entity_graph
        self.embedder = embedder or HashEmbedder()
        dim = getattr(self.embedder, "dim", 128)
        self.store = store if store is not None else make_store(home, dim=dim)
        self.stm: deque = deque(maxlen=20)

    def _point_id(self, text: str) -> str:
        # Full canonical UUID, not a 12-char hex slice. Newer
        # qdrant-client local mode validates point ids strictly
        # and rejects a 12-char hex id with
        # ValueError: Point id ... is not a valid UUID.
        # InMemoryStore and entity graph accept any string id,
        # so a full UUID is safe for both stores.
        return str(uuid.uuid5(uuid.NAMESPACE_URL,
                              text + str(time.time_ns())))


    def _record_with_vector(self, collection: str, text: str,
                            vector: list[float],
                            sparse, payload: dict) -> Point:
        data = dict(payload or {})
        data.setdefault("created_at", _now_iso())
        data.setdefault("last_accessed", data["created_at"])
        data.setdefault("last_access_day", _today_iso())
        data.setdefault("access_count", 1)
        data.setdefault("access_day_count", 1)
        point = Point(id=self._point_id(text), text=text, vector=vector,
                      payload=data, sparse=sparse or ([], []))
        self.store.upsert(collection, point)
        return point

    def record(self, collection: str, text: str,
               payload: dict | None = None) -> Point:
        # Automatic redaction at record time: credential-like
        # patterns are replaced before embedding and storage,
        # so a secret pasted into a goal never lands in Qdrant.
        # Email addresses are NOT redacted here, they are
        # legitimate recipients in specs and drafts.
        try:
            from .redaction import redact_dict, redact_text
            text = redact_text(text)
            payload = redact_dict(payload or {})
        except Exception:
            payload = payload or {}
        point = self._record_with_vector(
            collection, text, self.embedder.embed(text),
            self.embedder.embed_sparse(text), payload or {})
        if self.entity_graph is not None:
            try:
                self.entity_graph.index_point(collection, point.id, text, point.payload)
            except Exception:
                pass
        return point

    def record_image(self, collection: str, image_path,
                     description: str, payload: dict | None = None) -> Point:
        """Record an image point, with its file link in the payload.

        The image file itself stays on disk, under the Aidon
        home (library/ or knowledge/). The point holds the
        image vector (440M vision encoder, or a description
        vector with the hash fallback), the description text,
        and the link fields the caller sets: library_id,
        library_path, library_url, and kind=image. Recalling
        the text therefore returns the link, so the caller
        can show or open the image. File bytes are never
        stored in Qdrant.
        """
        data = {"kind": "image", "media": "image"}
        data.update(payload or {})
        vector = self.embedder.embed_image(image_path, description)
        return self._record_with_vector(
            collection, description, vector,
            self.embedder.embed_sparse(description), data)

    def record_stm(self, role: str, text: str,
                   attachments: list | None = None,
                   project_id: str = "", conversation_id: str = "") -> None:
        """Working memory: chat history, last 20 exchanges."""
        self.stm.append({"role": role, "text": text,
                         "attachments": list(attachments or [])})
        payload = {"role": role, "kind": "stm"}
        if attachments:
            payload["attachments"] = list(attachments)
        if project_id:
            payload["project_id"] = project_id
        if conversation_id:
            payload["conversation_id"] = conversation_id
        self.record("working", text, payload)

    def record_episodic(self, project_id: str, text: str,
                        payload: dict | None = None,
                        attachments: list | None = None) -> Point:
        data = {"project_id": project_id, "kind": "episodic",
                "promoted": False}
        data.update(payload or {})
        if attachments:
            data["attachments"] = list(attachments)
        point = self.record("episodic", text, data)
        try:
            from .scoring import ltm_eligibility
            score = ltm_eligibility(point.payload)
            point.payload["memory_score"] = score
            if hasattr(self.store, "set_payload"):
                self.store.set_payload("episodic", point.id,
                                       {"memory_score": score})
        except Exception:
            pass
        return point

    def remember(self, text: str, *, source: str = "explicit",
                 project_id: str = "") -> Point:
        """Pin a user-requested fact directly into global semantic LTM."""
        text = str(text or "").strip()
        if not text:
            raise ValueError("memory text is required")
        payload = {"kind": "semantic", "pinned": True,
                   "memory_source": source, "memory_score": 1.0}
        if project_id:
            payload["project_id"] = project_id
        return self.record("semantic", text, payload)

    def record_access(self, collection: str, point_id) -> None:
        """A recall is an access, best-effort, never failing recall.

        Takes a ``Point`` when the caller already has one, which avoids
        a retrieve round trip per hit. Updates payload only, so the
        vectors are never rewritten on a read.
        """
        try:
            point = point_id if isinstance(point_id, Point) \
                else self.store.get(collection, str(point_id))
            if point is None:
                return
            today = _today_iso()
            point.payload["access_count"] = int(point.payload.get("access_count", 0)) + 1
            if point.payload.get("last_access_day") != today:
                point.payload["access_day_count"] = int(point.payload.get("access_day_count", 0)) + 1
                point.payload["last_access_day"] = today
            point.payload["last_accessed"] = _now_iso()
            changed = {key: point.payload[key] for key in
                       ("access_count", "access_day_count",
                        "last_access_day", "last_accessed")}
            if hasattr(self.store, "set_payload"):
                self.store.set_payload(collection, point.id, changed)
            else:
                self.store.upsert(collection, point)
        except Exception:
            pass

    def recall(self, collection: str, query: str,
               limit: int = 5, graph: bool = True) -> list[Hit]:
        vector = self.embedder.embed_query(query)
        sparse = self.embedder.embed_sparse(query)
        hits = self.store.search(collection, vector, query, limit, sparse=sparse)
        hits = retention_rank(hits, len(hits))
        for hit in hits:
            self.record_access(collection, hit.point)
        if not graph:
            return hits
        seen = {h.point.id for h in hits}
        expanded: list[Hit] = []
        for hit in list(hits):
            for link_id in hit.point.payload.get("links", []):
                if link_id in seen:
                    continue
                linked = self.store.get(collection, link_id)
                if linked is not None:
                    seen.add(link_id)
                    expanded.append(Hit(linked, hit.score * 0.5, True))
        if self.entity_graph is not None:
            for hit in list(hits):
                for related in self.entity_graph.related(collection, hit.point.id, limit=5):
                    if related["point_id"] in seen:
                        continue
                    linked = self.store.get(related["collection"], related["point_id"])
                    if linked is not None:
                        seen.add(related["point_id"])
                        expanded.append(Hit(linked, hit.score * 0.5, True))
        return hits + expanded

    def recall_both(self, query: str, episode_limit: int = 5,
                    knowledge_limit: int = 5):
        """One embed, two recalls: episodic + knowledge, same query.

        Episodes over-fetch (5x) and are retention-ranked before
        truncation, so an important extracted fact can win the
        episode slot over a merely similar episode. The limit stays
        a display budget for the plan prompt; the ranking decides
        which memories are retained into the next phase.
        """
        vector = self.embedder.embed_query(query)
        sparse = self.embedder.embed_sparse(query)
        over = episode_limit * 5 if episode_limit else 0
        episodes = self.store.search("episodic", vector, query, over, sparse=sparse) if over else []
        episodes = retention_rank(episodes, episode_limit)
        knowledge = self.store.search("knowledge", vector, query, knowledge_limit, sparse=sparse)
        for hit in episodes:
            self.record_access("episodic", hit.point)
        for hit in knowledge:
            self.record_access("knowledge", hit.point)
        return episodes, knowledge

    def forget_search(self, query: str, limit: int = 10) -> dict:
        """Semantic search over episodic + knowledge: what would be deleted."""
        episodes, knowledge = self.recall_both(query, limit, limit)
        return {"episodic": episodes, "knowledge": knowledge}

    def forget(self, collection: str, point_id: str) -> bool:
        return self.store.delete(collection, point_id)

    def forget_project(self, project_id: str) -> dict[str, int]:
        return {name: self.store.delete_by_project(name, project_id)
                for name in COLLECTIONS}

    def prune(self, collection: str, *, half_life_days: float = 21.0,
              threshold: float = 0.02, grace_days: int = 35) -> list[str]:
        """Explicit prune only: returns removed point ids, never auto."""
        removed = []
        for point in self.store.iter_points(collection):
            if should_prune(point.payload, half_life_days=half_life_days,
                             threshold=threshold, grace_days=grace_days):
                if self.store.delete(collection, point.id):
                    removed.append(point.id)
        return removed

    def promote_to_ltm(self, episodic_point: Point,
                       kind: str = "semantic") -> Point | None:
        """Copy an episodic point to LTM: deduped or superseding.

        Memory needs no approval. An identical text already in
        LTM is a duplicate: nothing new is recorded, the
        episodic point is just marked promoted. A point
        sharing at least half its shape tokens is the same
        memory in new words, so the old copy is deleted and
        the new one recorded — the same bar experience
        merging uses. Anything else is recorded alongside.
        """
        if kind not in ("semantic", "experience"):
            return None
        try:
            hits = self.recall(kind, episodic_point.text,
                               limit=5, graph=False)
        except Exception:
            hits = []
        new_shape = _text_shape(episodic_point.text)
        best, best_overlap, best_text = None, 0.0, ""
        for hit in hits:
            point = getattr(hit, "point", None)
            if point is None:
                continue
            payload = point.payload or {}
            old_text = str(getattr(point, "text", "")
                           or payload.get("text", "") or "")
            overlap = _shape_overlap(new_shape, _text_shape(old_text))
            if overlap > best_overlap:
                best, best_overlap, best_text = point, overlap, old_text
        if best is not None and (
                best_overlap >= 1.0
                or best_text.strip() == str(episodic_point.text or "").strip()):
            self._mark_promoted(episodic_point)
            return best
        if best is not None and best_overlap >= _PROMOTE_SUPERSEDE_SHAPE:
            try:
                self.store.delete(kind, best.id)
            except Exception:
                pass
        target = self.record(kind, episodic_point.text,
                             {**episodic_point.payload,
                              "promoted_from": episodic_point.id,
                              "kind": kind})
        self._mark_promoted(episodic_point)
        return target

    def _mark_promoted(self, episodic_point: Point) -> None:
        episodic_point.payload["promoted"] = True
        if hasattr(self.store, "set_payload"):
            self.store.set_payload("episodic", episodic_point.id,
                                   {"promoted": True})
        else:
            self.store.upsert("episodic", episodic_point)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return self.store.list_points(collection, limit)

    def iter_points(self, collection: str):
        """Every point in a collection, paged, for export and prune."""
        if hasattr(self.store, "iter_points"):
            return self.store.iter_points(collection)
        return iter(self.store.list_points(collection, limit=100000))

    def conversation_history(self, conversation_id: str,
                             limit: int = 100) -> list[Point]:
        """Working points for one conversation, oldest first, Qdrant-backed."""
        if hasattr(self.store, "list_filtered"):
            return self.store.list_filtered(
                "working", {"conversation_id": conversation_id}, limit=limit)
        return [p for p in self.store.list_points("working", limit=limit)
                if p.payload.get("conversation_id") == conversation_id]

    def conversation_context(self, conversation_id: str,
                             max_exchanges: int = 20) -> dict:
        """Last exchanges for a conversation, plus a stored summary if any."""
        history = self.conversation_history(conversation_id, limit=1000)
        summary = [p for p in history if p.payload.get("kind") == "summary"]
        exchanges = [p for p in history if p.payload.get("kind") != "summary"]
        return {
            "exchanges": exchanges[-max_exchanges:],
            "summary": summary[-1].text if summary else "",
            "summary_point_id": summary[-1].id if summary else "",
        }

    def record_summary(self, conversation_id: str, summary_text: str,
                       project_id: str = "") -> Point:
        """Store a conversation summary point, model-written by the caller."""
        payload = {"kind": "summary", "conversation_id": conversation_id}
        if project_id:
            payload["project_id"] = project_id
        return self.record("working", summary_text, payload)

    def list_projects(self) -> list[str]:
        """Distinct project_ids across working, episodic, and library scope."""
        ids = set()
        for collection in ("working", "episodic"):
            for point in self.store.iter_points(collection):
                pid = point.payload.get("project_id")
                if pid:
                    ids.add(pid)
        return sorted(ids)
