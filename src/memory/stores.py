"""Memory point models, hybrid ranking, and vector-store adapters."""

from __future__ import annotations

import atexit
import json
import heapq
import math
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .embedder import keyword_sparse_vector, sparse_weights

COLLECTIONS = ("working", "episodic", "semantic", "experience",
               "knowledge")
_RRF_K = 60


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
    """Re-rank recall hits with a restrained extraction-importance bonus."""
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


def _hybrid_search(points, vector, query_text, limit, sparse=None):
    """Shared fallback ranking; select only the ranks consumed by RRF."""
    if limit <= 0:
        return []
    points = list(points)
    over = max(limit * 2, 4)
    query_sparse = dict(zip(sparse[0], sparse[1])) if sparse else {}
    if not query_sparse:
        query_sparse = dict(zip(*_sparse_from_text(query_text)))

    def sparse_score(point):
        point_sparse = (dict(zip(point.sparse[0], point.sparse[1]))
                        if point.sparse and point.sparse[0]
                        else dict(zip(*_sparse_from_text(point.text))))
        return sum(min(query_sparse.get(i, 0.0), weight)
                   for i, weight in point_sparse.items())

    dense = heapq.nlargest(over, points, key=lambda point: _cosine(vector, point.vector))
    keyword = heapq.nlargest(over, points, key=sparse_score)
    return _rrf(dense, keyword, limit)


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
        return _hybrid_search(self._data[collection].values(), vector, query_text, limit, sparse)


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
                (collection, point.id, point.text,
                 json.dumps([float(value) for value in point.vector]),
                 json.dumps([[int(index) for index in point.sparse[0]],
                             [float(value) for value in point.sparse[1]]]),
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
        with self._lock:
            point = self.get(collection, point_id)
            if point is None:
                return
            point.payload.update(payload)
            self.upsert(collection, point)

    def _rows(self, collection: str, limit: int | None = None,
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
        return _hybrid_search(self._rows(collection), vector, query_text, limit, sparse)


class SQLiteFaissStore(SQLiteStore):
    """SQLite source of truth with rebuildable FAISS dense indexes.

    SQLite persists points and sparse vectors. FAISS indexes are rebuilt
    from SQLite on startup, so a missing or damaged index never loses data.
    Sparse ranking remains SQLite-backed and is fused with FAISS dense
    candidates using the same RRF ranking as the other stores.
    """

    def __init__(self, home: Path, dim: int = 768):
        super().__init__(home)
        import faiss
        import numpy as np

        self._faiss = faiss
        self._np = np
        self._default_dim = int(dim)
        self._indexes = {}
        self._index_dims = {}
        self._faiss_broken: set[str] = set()
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS faiss_point_ids ("
            "collection TEXT NOT NULL, id TEXT NOT NULL, faiss_id INTEGER NOT NULL, "
            "PRIMARY KEY(collection, id), UNIQUE(collection, faiss_id))")
        self._db.commit()
        for collection in COLLECTIONS:
            self._rebuild_index(collection)

    def _faiss_id(self, collection: str, point_id: str) -> int:
        row = self._db.execute(
            "SELECT faiss_id FROM faiss_point_ids WHERE collection=? AND id=?",
            (collection, str(point_id))).fetchone()
        if row is not None:
            return int(row[0])
        next_id = self._db.execute(
            "SELECT COALESCE(MAX(faiss_id), 0) + 1 FROM faiss_point_ids "
            "WHERE collection=?", (collection,)).fetchone()[0]
        self._db.execute(
            "INSERT INTO faiss_point_ids(collection,id,faiss_id) VALUES(?,?,?)",
            (collection, str(point_id), int(next_id)))
        self._db.commit()
        return int(next_id)

    def _make_index(self, collection: str, dim: int):
        self._indexes[collection] = self._faiss.IndexIDMap2(
            self._faiss.IndexFlatIP(dim))
        self._index_dims[collection] = dim

    def _normalized(self, vector):
        array = self._np.asarray(vector, dtype=self._np.float32).reshape(1, -1)
        self._faiss.normalize_L2(array)
        return array

    def _rebuild_index(self, collection: str) -> None:
        points = self._rows(collection)
        dim = len(points[0].vector) if points else self._default_dim
        self._make_index(collection, dim)
        try:
            for point in points:
                if len(point.vector) != dim:
                    raise ValueError(
                        f"mixed vector dimensions in {collection}: "
                        f"expected {dim}, got {len(point.vector)}")
                self._indexes[collection].add_with_ids(
                    self._normalized(point.vector),
                    self._np.asarray([self._faiss_id(collection, point.id)],
                                     dtype=self._np.int64))
        except Exception:
            self._faiss_broken.add(collection)

    def upsert(self, collection: str, point: Point) -> None:
        with self._lock:
            super().upsert(collection, point)
            if collection in self._faiss_broken:
                return
            try:
                dim = self._index_dims.get(collection, self._default_dim)
                index = self._indexes[collection]
                if index.ntotal == 0 and len(point.vector) != dim:
                    dim = len(point.vector)
                    self._make_index(collection, dim)
                    index = self._indexes[collection]
                if len(point.vector) != dim:
                    raise ValueError(
                        f"vector dimension changed in {collection}: "
                        f"expected {dim}, got {len(point.vector)}")
                faiss_id = self._faiss_id(collection, point.id)
                index.remove_ids(self._np.asarray([faiss_id], dtype=self._np.int64))
                index.add_with_ids(self._normalized(point.vector),
                                   self._np.asarray([faiss_id], dtype=self._np.int64))
            except Exception:
                # SQLite already committed the point. Search can still use its
                # exact dense+sparse fallback without risking data loss.
                self._faiss_broken.add(collection)

    def delete(self, collection: str, point_id: str) -> bool:
        with self._lock:
            removed = super().delete(collection, point_id)
            if not removed:
                return False
            if collection in self._faiss_broken:
                return True
            try:
                row = self._db.execute(
                    "SELECT faiss_id FROM faiss_point_ids WHERE collection=? AND id=?",
                    (collection, str(point_id))).fetchone()
                if row is not None:
                    self._indexes[collection].remove_ids(
                        self._np.asarray([int(row[0])], dtype=self._np.int64))
                    self._db.execute(
                        "DELETE FROM faiss_point_ids WHERE collection=? AND id=?",
                        (collection, str(point_id)))
                    self._db.commit()
            except Exception:
                self._faiss_broken.add(collection)
            return True

    def search(self, collection: str, vector: list[float], query_text: str,
               limit: int = 5,
               sparse: tuple[list[int], list[float]] | None = None) -> list[Hit]:
        if limit <= 0:
            return []
        if collection in self._faiss_broken:
            return _hybrid_search(self._rows(collection), vector, query_text,
                                  limit, sparse)
        try:
            with self._lock:
                points = self._rows(collection)
                if not points:
                    return []
                index = self._indexes[collection]
                if len(vector) != self._index_dims[collection]:
                    raise ValueError("query vector dimension does not match FAISS index")
                over = max(limit * 2, 4)
                _scores, ids = index.search(self._normalized(vector), over)
                by_faiss_id = {self._faiss_id(collection, p.id): p for p in points}
            dense = [by_faiss_id[int(fid)] for fid in ids[0]
                     if int(fid) in by_faiss_id]
            query_sparse = dict(zip(sparse[0], sparse[1])) if sparse else {}
            if not query_sparse:
                query_sparse = dict(zip(*_sparse_from_text(query_text)))

            def sparse_score(point):
                point_sparse = (dict(zip(point.sparse[0], point.sparse[1]))
                                if point.sparse and point.sparse[0]
                                else dict(zip(*_sparse_from_text(point.text))))
                return sum(min(query_sparse.get(i, 0.0), weight)
                           for i, weight in point_sparse.items())

            keyword = heapq.nlargest(over, points, key=sparse_score)
            return _rrf(dense, keyword, limit)
        except Exception:
            self._faiss_broken.add(collection)
            return _hybrid_search(self._rows(collection), vector, query_text,
                                  limit, sparse)


def _sparse_from_text(text: str):
    # Keyword sparse, via the base embedder helper, without
    # constructing an embedder instance.
    return keyword_sparse_vector(text)


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
        or conversation discovery silently skipped everything past the cap.
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
    """Select SQLite+FAISS first, Qdrant second, and plain SQLite last.

    ``AIDON_MEMORY_BACKEND`` may force ``auto``, ``sqlite-faiss``,
    ``qdrant``, or ``sqlite``. Automatic fallback occurs during startup;
    the stores are not dual-written, so this is not transparent failover
    after a running backend fails.
    """
    import warnings

    selected = os.environ.get("AIDON_MEMORY_BACKEND", "auto").strip().lower()
    if selected not in {"auto", "sqlite-faiss", "qdrant", "sqlite"}:
        raise ValueError("AIDON_MEMORY_BACKEND must be auto, sqlite-faiss, qdrant, or sqlite")
    if selected == "sqlite":
        return SQLiteStore(home)
    if selected == "sqlite-faiss":
        return SQLiteFaissStore(home, dim=dim)
    url = os.environ.get("AIDON_QDRANT_URL", "").strip()
    if selected == "qdrant":
        return QdrantStore(home, url=url, dim=dim)

    try:
        return SQLiteFaissStore(home, dim=dim)
    except Exception as exc:
        warnings.warn(
            f"SQLite+FAISS memory unavailable ({type(exc).__name__}: {exc}); "
            "trying Qdrant", RuntimeWarning)
    try:
        return QdrantStore(home, url=url, dim=dim)
    except Exception as exc:
        warnings.warn(
            f"Qdrant memory unavailable ({type(exc).__name__}: {exc}); "
            "using persistent SQLite", RuntimeWarning)
        return SQLiteStore(home)


