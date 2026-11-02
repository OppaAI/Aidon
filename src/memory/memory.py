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
- Episodic memory: chat-specific memory. Interview
  questions and answers, and specific information and data
  within chat sessions, scoped by conversation_id, in the
  ``episodic`` collection. This is what a chat remembers
  about itself, not what Aidon knows in general.
- Semantic memory (LTM, global): facts, in the ``semantic``
  collection. Not chat-scoped, reusable across chats.
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
leaves episodic memories chat-scoped only. Promotion
copies a point to the semantic collection (for a fact) or
to the experience collection (for a workflow), and marks
the episodic point as promoted, it does not delete the
episodic original, and it copies the payload, so a library
link survives promotion.

Chats, without a second store:
Conversation ids are payload fields on working
and episodic points. ``conversation_history``
reads them back from Qdrant by filtered
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

import time
import uuid
from collections import deque
from pathlib import Path

from .embedder import Embedder, HashEmbedder, tokens

from .stores import (COLLECTIONS, Hit, InMemoryStore, Point, QdrantStore,
                    SQLiteStore, make_store, retention_rank)
from .retention import (decay_score, is_grace_protected, should_prune,
                        _now_iso, _today_iso)


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
                   conversation_id: str = "") -> None:
        """Working memory: chat history, last 20 exchanges."""
        self.stm.append({"role": role, "text": text,
                         "attachments": list(attachments or [])})
        payload = {"role": role, "kind": "stm"}
        if attachments:
            payload["attachments"] = list(attachments)
        if conversation_id:
            payload["conversation_id"] = conversation_id
        self.record("working", text, payload)

    def record_episodic(self, conversation_id: str, text: str,
                        payload: dict | None = None,
                        attachments: list | None = None) -> Point:
        data = {"conversation_id": conversation_id, "kind": "episodic",
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

    def remember(self, text: str, *, source: str = "explicit") -> Point:
        """Pin a user-requested fact directly into global semantic LTM."""
        text = str(text or "").strip()
        if not text:
            raise ValueError("memory text is required")
        payload = {"kind": "semantic", "pinned": True,
                   "memory_source": source, "memory_score": 1.0}
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

    def record_summary(self, conversation_id: str, summary_text: str) -> Point:
        """Store a conversation summary point, model-written by the caller."""
        payload = {"kind": "summary", "conversation_id": conversation_id}
        return self.record("working", summary_text, payload)


