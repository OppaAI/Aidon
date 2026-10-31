"""Entity graph for memory and KB: find by shared entity, not only by vector.

The memory design is explicit: "No entity graphs, no spreading; just
hybrid recall." The implementation keeps dense plus sparse fused with RRF,
and adds the
entity graph on top, so a point that is relevant because it
shares an entity with a recalled point is found even when its
vector score is low.

What is an entity, here, no model call, no NER model:
- a capitalised phrase, for example "Northside Bakery" or
  "Save-On-Foods", found by regex, case-normalised for the index,
- a file name, for example worm.py or menu.pdf,
- a conversation id, goal word, or tool name, when it appears in
  the point payload, because those are the entities Aidon
  already uses to organise work.

How it is stored:
- <home>/entity_graph.json, entity (lowercased) to a list of
  (collection, point_id) pairs, sorted, deduplicated. It is a
  file, not a new database, so it survives with the home, and
  it is rebuilt by re-indexing points, never by a model.
- At record time, Memory indexes a point's entities, when a
  graph is attached, so memory and KB points both enter the
  graph, because KB ingest records through Memory.
- At recall time, Memory expands a hit by its entities: points
  sharing at least one entity, scored by shared entity count,
  added with score times 0.5, marked graph_expanded, deduplicated
  against the original hits and against link expansion. A point
  is never returned twice, and an entity expansion never
  outranks a direct vector hit, because its score is halved.

Why it helps: a query for "Worm movement" finds the Worm game
point by vector, and the entity graph then finds the Worm
test point, the Worm project point, and the KB point about
Worm collision, because they share the entity Worm, even if
their text does not contain the word movement. Vector recall
finds similar words, entity recall finds the same thing.
"""

from __future__ import annotations

import atexit
import json
import re
import time
import weakref
from pathlib import Path

_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z0-9]+(?:[ -][A-Z][A-Za-z0-9]+)+\b|\b[A-Z][a-z]{2,}\b")
_FILE = re.compile(r"\b[\w\-]+\.(?:py|md|pdf|json|csv|txt|html|js|ts)\b")
_STOP_ENTITIES = {"The", "This", "That", "With", "From", "Goal", "Write", "Make", "Create"}


def extract_entities(text: str, payload: dict | None = None) -> list[str]:
    """Extract entities from text and payload, lowercased, deduplicated, sorted."""
    entities: set[str] = set()
    for match in _CAPITALISED.findall(text or ""):
        if match not in _STOP_ENTITIES:
            entities.add(match.lower())
    for match in _FILE.findall(text or ""):
        entities.add(match.lower())
    if payload:
        for key in ("conversation_id", "goal", "deliverable", "file"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                entities.update(extract_entities(value))
        for tool in payload.get("tools_used", []) if isinstance(payload.get("tools_used"), list) else []:
            if isinstance(tool, str):
                entities.add(tool.lower())
    return sorted(entities)


class EntityGraph:
    def __init__(self, home: Path, save_interval: float = 1.0):
        self.path = Path(home) / "entity_graph.json"
        self.index: dict[str, list[list[str]]] = {}
        # Reverse index, (collection, point_id) -> entities. Without it
        # every entities_for_point() call scanned the whole graph, and
        # recall calls it once per hit, so recall got slower as memory
        # grew.
        self._by_point: dict[tuple[str, str], set[str]] = {}
        self._dirty = False
        self._last_save = 0.0
        # Writes are debounced. Saving on every point rewrites the whole
        # file, so ingesting n points costs O(n^2) bytes. The graph is
        # rebuilt by re-indexing points, so losing the last second on a
        # hard kill is acceptable; a clean exit flushes it.
        self._save_interval = save_interval
        if self.path.is_file():
            try:
                self.index = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.index = {}
        for entity, pairs in self.index.items():
            for pair in pairs:
                if isinstance(pair, list) and len(pair) == 2:
                    self._by_point.setdefault(
                        (str(pair[0]), str(pair[1])), set()).add(entity)
        _GRAPHS.add(self)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Compact JSON, no indent: pretty-printing a growing graph costs
        # more than the data itself on every save.
        self.path.write_text(
            json.dumps(self.index, separators=(",", ":")),
            encoding="utf-8")
        self._dirty = False
        self._last_save = time.monotonic()

    def flush(self) -> None:
        """Write pending changes now, used at shutdown and by callers."""
        if self._dirty:
            self.save()

    def _maybe_save(self) -> None:
        if time.monotonic() - self._last_save >= self._save_interval:
            self.save()
        else:
            self._dirty = True

    def index_point(self, collection: str, point_id: str, text: str, payload: dict | None = None) -> list[str]:
        entities = extract_entities(text, payload)
        key = (collection, point_id)
        known = self._by_point.setdefault(key, set())
        for entity in entities:
            if entity in known:
                continue
            known.add(entity)
            self.index.setdefault(entity, []).append([collection, point_id])
        if entities:
            self._maybe_save()
        return entities

    def points_for(self, entity: str) -> list[list[str]]:
        return [list(pair) for pair in self.index.get(entity.lower(), [])]

    def entities_for_point(self, collection: str, point_id: str) -> list[str]:
        return sorted(self._by_point.get((collection, point_id), set()))

    def related(self, collection: str, point_id: str, limit: int = 5) -> list[dict]:
        """Points sharing entities with the given point, by shared count."""
        counts: dict[tuple[str, str], int] = {}
        for entity in self.entities_for_point(collection, point_id):
            for pair in self.index.get(entity, []):
                key = (pair[0], pair[1])
                if key == (collection, point_id):
                    continue
                counts[key] = counts.get(key, 0) + 1
        # Deterministic tie-break, so the same graph always ranks the same.
        ranked = sorted(counts.items(),
                        key=lambda kv: (-kv[1], kv[0][0], kv[0][1]))
        return [{"collection": coll, "point_id": pid, "shared_entities": count}
                for (coll, pid), count in ranked[:limit]]


_GRAPHS: "weakref.WeakSet[EntityGraph]" = weakref.WeakSet()


def _flush_all() -> None:
    for graph in list(_GRAPHS):
        try:
            graph.flush()
        except Exception:
            pass


atexit.register(_flush_all)
