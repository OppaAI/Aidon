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

Promotion, episodic -> LTM:
At the end of a completed run, Aidon may ask the user whether
to put that run's episodic memories into LTM. If the
environment flag AIDON_AUTO_PROMOTE_TO_LTM is "1", promotion
happens automatically, without asking. Otherwise the default
is to ask, and a "no" leaves episodic memories project-scoped
only. Promotion copies a point to the semantic collection
(for a fact) or to the experience collection (for a workflow),
and marks the episodic point as promoted, it does not delete
the episodic original.

Other mechanisms, same as the earlier design:
- record: upsert a point with text, vector, and payload.
- recall: vector search plus keyword overlap, with an
  optional one-hop knowledge-graph expansion: a point whose
  payload has ``links`` (other point ids) pulls those points
  in as graph-expanded results.
- forget: delete a point by id, or all points for a project,
  or all points in a collection.
- list / prune: list points in a collection, prune old
  episodic points by age when asked, never automatically.

If qdrant_client is not installed, the store falls back to
an in-memory store with the same collection / point / search
shape, so tests run offline. When qdrant_client is installed,
the real Qdrant store is used, local mode by default.

Images: see library.py. A memory point never holds image
bytes; it holds the image vector and the library link, so
recall returns the file reference alongside the text.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .embedder import Embedder, HashEmbedder, sparse_weights

COLLECTIONS = ("working", "episodic", "semantic", "experience",
               "knowledge")


@dataclass
class Point:
    id: str
    text: str
    vector: list[float]
    payload: dict = field(default_factory=dict)


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

    def search(self, collection: str, vector: list[float],
               query_text: str, limit: int = 5) -> list[Hit]:
        query_tokens = sparse_weights(query_text)
        scored: list[Hit] = []
        for point in self._data[collection].values():
            dense = _cosine(vector, point.vector)
            point_tokens = sparse_weights(point.text)
            overlap = sum(min(query_tokens.get(t, 0.0), w)
                          for t, w in point_tokens.items())
            keyword = overlap / (sum(query_tokens.values()) or 1.0)
            scored.append(Hit(point, 0.7 * dense + 0.3 * keyword))
        scored.sort(key=lambda h: h.score, reverse=True)
        return scored[:limit]


class QdrantStore:
    """Real Qdrant store, local mode by default, or server URL.

    Local mode: QdrantClient(path=<home>/qdrant). Server mode:
    QdrantClient(url=AIDON_QDRANT_URL). Points are stored with
    a dense vector and a payload that includes text, sparse
    keyword tokens, and links, so recall can do the same
    dense + keyword + graph expansion as the in-memory store.
    """

    def __init__(self, home: Path, url: str = "", dim: int = 768):
        from qdrant_client import QdrantClient, models

        self._models = models
        if url:
            self._client = QdrantClient(url=url)
        else:
            self._client = QdrantClient(path=str(Path(home) / "qdrant"))
        # Vector size must match the embedder: 768 for
        # EmbeddingGemma 2, 128 for HashEmbedder (tests). A
        # collection created with a different size cannot hold
        # the new vectors; a fresh home, or a recreated
        # collection, is the migration, never a silent resize.
        self._embedder_dim = dim
        for name in COLLECTIONS:
            if not self._client.collection_exists(name):
                self._client.create_collection(
                    name, vectors_config=models.VectorParams(
                        size=self._embedder_dim,
                        distance=models.Distance.COSINE))

    def _to_point(self, collection: str, raw) -> Point:
        payload = dict(raw.payload or {})
        text = payload.get("text", "")
        return Point(id=str(raw.id), text=text,
                     vector=list(raw.vector or []), payload=payload)

    def upsert(self, collection: str, point: Point) -> None:
        self._client.upsert(collection, points=[
            self._models.PointStruct(
                id=point.id, vector=point.vector,
                payload={**point.payload, "text": point.text,
                         "tokens": sparse_weights(point.text)})])

    def get(self, collection: str, point_id: str) -> Point | None:
        try:
            raw = self._client.retrieve(collection, ids=[point_id],
                                        with_vectors=True)
        except Exception:
            return None
        return self._to_point(collection, raw[0]) if raw else None

    def delete(self, collection: str, point_id: str) -> bool:
        try:
            self._client.delete(collection, points_selector=[point_id])
            return True
        except Exception:
            return False

    def delete_by_project(self, collection: str, project_id: str) -> int:
        # Qdrant delete by filter, then count by scroll is not
        # atomic in one call; scroll, filter, delete, count.
        points, _ = self._client.scroll(
            collection, scroll_filter=self._models.Filter(
                must=[self._models.FieldCondition(
                    key="project_id",
                    match=self._models.MatchValue(value=project_id))]),
            limit=1000, with_vectors=False)
        if not points:
            return 0
        self._client.delete(collection,
                            points_selector=[p.id for p in points])
        return len(points)

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        points, _ = self._client.scroll(collection, limit=limit,
                                        with_vectors=True)
        return [self._to_point(collection, p) for p in points]

    def search(self, collection: str, vector: list[float],
               query_text: str, limit: int = 5) -> list[Hit]:
        raw = self._client.query_points(
            collection, query=vector, limit=limit,
            with_payload=True, with_vectors=True).points
        return [Hit(self._to_point(collection, p), float(p.score or 0.0))
                for p in raw]


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
                 store=None):
        self.home = Path(home)
        self.embedder = embedder or HashEmbedder()
        dim = getattr(self.embedder, "dim", 128)
        self.store = store if store is not None else make_store(home,
                                                                 dim=dim)
        self.stm: deque = deque(maxlen=20)

    def _point_id(self, text: str) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL,
                          text + str(time.time_ns())).hex[:12]

    def record(self, collection: str, text: str,
               payload: dict | None = None) -> Point:
        point = Point(id=self._point_id(text),
                      text=text, vector=self.embedder.embed(text),
                      payload=dict(payload or {}))
        self.store.upsert(collection, point)
        return point

    def record_image(self, collection: str, image_path,
                     description: str, payload: dict | None = None) -> Point:
        """Record an image point, with its file link in the payload.

        The image file itself stays on disk, under the Aidon
        home (library/ or knowledge/). The point holds the
        image vector (440M vision encoder, or a description
        vector with the hash fallback), the description text,
        and the link fields the caller sets: library_id,
        library_path (relative to home), library_url
        (the future WebUI route, /api/library/<id>/file), and
        kind=image. Recalling the text therefore returns the
        link, so the caller can show or open the image.
        The file bytes are never stored in Qdrant.
        """
        data = {"kind": "image", "media": "image"}
        data.update(payload or {})
        vector = self.embedder.embed_image(image_path, description)
        point = Point(id=self._point_id(description + str(image_path)),
                      text=description, vector=vector, payload=data)
        self.store.upsert(collection, point)
        return point

    def record_stm(self, role: str, text: str,
                   attachments: list | None = None) -> None:
        """Working memory: chat history, last 20 exchanges.

        ``attachments`` is a list of library item ids (or
        library link dicts) attached to this exchange, so a
        recalled chat exchange can refer to its images.
        """
        self.stm.append({"role": role, "text": text,
                         "attachments": list(attachments or [])})
        payload = {"role": role, "kind": "stm"}
        if attachments:
            payload["attachments"] = list(attachments)
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

    def recall(self, collection: str, query: str,
               limit: int = 5, graph: bool = True) -> list[Hit]:
        vector = self.embedder.embed_query(query)
        hits = self.store.search(collection, vector, query, limit)
        if not graph:
            return hits
        # Knowledge-graph expansion, one hop: linked points join
        # the results, marked as graph-expanded, not as direct hits.
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
        return hits + expanded

    def forget(self, collection: str, point_id: str) -> bool:
        return self.store.delete(collection, point_id)

    def forget_project(self, project_id: str) -> dict[str, int]:
        return {name: self.store.delete_by_project(name, project_id)
                for name in COLLECTIONS}

    def promote_to_ltm(self, episodic_point: Point,
                       kind: str = "semantic") -> Point | None:
        """Copy an episodic point to LTM. kind: semantic | experience.

        Returns the new LTM point, or None if kind is unknown.
        The episodic original is kept and marked promoted.
        """
        if kind not in ("semantic", "experience"):
            return None
        target = self.record(kind, episodic_point.text,
                             {**episodic_point.payload,
                              "promoted_from": episodic_point.id,
                              "kind": kind})
        # Mark the episodic original as promoted, in place.
        episodic_point.payload["promoted"] = True
        self.store.upsert("episodic", episodic_point)
        return target

    @staticmethod
    def auto_promote_enabled() -> bool:
        """Env flag: AIDON_AUTO_PROMOTE_TO_LTM=1 means auto-promote,
        otherwise ask the user at the end of a completed run."""
        return os.environ.get("AIDON_AUTO_PROMOTE_TO_LTM", "0") == "1"

    def list_points(self, collection: str, limit: int = 50) -> list[Point]:
        return self.store.list_points(collection, limit)
