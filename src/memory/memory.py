"""Memory and knowledge framework, Qdrant for all.

One database, Qdrant, no second database dependency. One
Qdrant instance, local mode under the Aidon home by default,
or a server URL if AIDON_QDRANT_URL is set. Different
collections, same instance, so forget, recall, and promotion
can be scoped by collection without a second store.

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

import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .embedder import Embedder, HashEmbedder, sparse_weights

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

    def delete_by_project(self, collection: str, project_id: str) -> int:
        doomed = [pid for pid, p in self._data[collection].items()
                  if p.payload.get("project_id") == project_id]
        for pid in doomed:
            del self._data[collection][pid]
        return len(doomed)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return list(self._data[collection].values())[:limit]

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


def _sparse_from_text(text: str):
    from .embedder import Embedder as _E
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
    fresh home, or a recreated collection, is the migration,
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
        if isinstance(raw.vector, dict):
            vector = list(raw.vector.get("dense") or [])
        elif raw.vector is not None:
            vector = list(raw.vector)
        return Point(id=str(raw.id), text=text, vector=vector, payload=payload)

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
        points = self._scroll_filtered(collection, {"project_id": project_id}, 1000)
        if not points:
            return 0
        self._client.delete(collection, points_selector=[p.id for p in points])
        return len(points)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        points, _ = self._client.scroll(collection, limit=limit, with_vectors=True)
        return [self._to_point(p) for p in points]

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
    """Real Qdrant if qdrant_client is installed, else in-memory."""
    try:
        import qdrant_client  # noqa: F401
    except ImportError:
        return InMemoryStore()
    url = os.environ.get("AIDON_QDRANT_URL", "").strip()
    return QdrantStore(home, url=url, dim=dim)


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

    def _access_payload(self) -> dict:
        now = _now_iso()
        return {"created_at": now, "last_accessed": now,
                "last_access_day": _today_iso(),
                "access_count": 1, "access_day_count": 1}

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
        return self.record("episodic", text, data)

    def record_access(self, collection: str, point_id: str) -> None:
        """A recall is an access, best-effort, never failing recall."""
        try:
            point = self.store.get(collection, point_id)
            if point is None:
                return
            today = _today_iso()
            point.payload["access_count"] = int(point.payload.get("access_count", 0)) + 1
            if point.payload.get("last_access_day") != today:
                point.payload["access_day_count"] = int(point.payload.get("access_day_count", 0)) + 1
                point.payload["last_access_day"] = today
            point.payload["last_accessed"] = _now_iso()
            self.store.upsert(collection, point)
        except Exception:
            pass

    def recall(self, collection: str, query: str,
               limit: int = 5, graph: bool = True) -> list[Hit]:
        vector = self.embedder.embed_query(query)
        sparse = self.embedder.embed_sparse(query)
        hits = self.store.search(collection, vector, query, limit, sparse=sparse)
        for hit in hits:
            self.record_access(collection, hit.point.id)
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
        """One embed, two recalls: episodic + knowledge, same query."""
        vector = self.embedder.embed_query(query)
        sparse = self.embedder.embed_sparse(query)
        episodes = self.store.search("episodic", vector, query, episode_limit, sparse=sparse)
        knowledge = self.store.search("knowledge", vector, query, knowledge_limit, sparse=sparse)
        for hit in episodes:
            self.record_access("episodic", hit.point.id)
        for hit in knowledge:
            self.record_access("knowledge", hit.point.id)
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
        for point in self.store.list_points(collection, limit=1000):
            if should_prune(point.payload, half_life_days=half_life_days,
                             threshold=threshold, grace_days=grace_days):
                if self.store.delete(collection, point.id):
                    removed.append(point.id)
        return removed

    def promote_to_ltm(self, episodic_point: Point,
                       kind: str = "semantic") -> Point | None:
        """Copy an episodic point to LTM, payload copied, link survives."""
        if kind not in ("semantic", "experience"):
            return None
        target = self.record(kind, episodic_point.text,
                             {**episodic_point.payload,
                              "promoted_from": episodic_point.id,
                              "kind": kind})
        episodic_point.payload["promoted"] = True
        self.store.upsert("episodic", episodic_point)
        return target

    def promote_with_ask(self, episodic_point: Point, ask,
                         kind: str = "semantic") -> Point | None:
        """Promotion with a caller-supplied ask callback.

        If auto-promote is on, promote without asking.
        Otherwise ask() must return True to promote; a False
        or an exception leaves episodic project-scoped only.
        ask is a chat question, not an approval card.
        """
        if self.auto_promote_enabled():
            return self.promote_to_ltm(episodic_point, kind=kind)
        try:
            approved = bool(ask(episodic_point))
        except Exception:
            approved = False
        return self.promote_to_ltm(episodic_point, kind=kind) if approved else None

    @staticmethod
    def auto_promote_enabled() -> bool:
        return os.environ.get("AIDON_AUTO_PROMOTE_TO_LTM", "0") == "1"

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return self.store.list_points(collection, limit)

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
            for point in self.store.list_points(collection, limit=1000):
                pid = point.payload.get("project_id")
                if pid:
                    ids.add(pid)
        return sorted(ids)
