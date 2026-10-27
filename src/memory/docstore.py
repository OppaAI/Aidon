"""Searchable indexes over uploaded documents (run working material).

A large uploaded file — a book, a long contract, a fat
report — is chunked and indexed into its own collection,
keyed by the content hash. The index answers pointed
questions over the document, while whole-document work
(summaries, totals) still walks the file sequentially —
the index is for questions, never a coverage substitute.

Deliberately separate from the memory collections:
uploads are working material for goals, not durable
knowledge. Dropping a document's collection forgets it
completely.

Storage: an in-memory dict by default. A Qdrant store
is used only when qdrant_client is importable AND a
store object is supplied; an import failure falls back
to memory, it never breaks ingestion.
"""

from __future__ import annotations

import hashlib
import re

DOC_PREFIX = "aidon_doc_"
CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def collection_for(text: str) -> str:
    return DOC_PREFIX + content_hash(text)[:16]


def _hard_split(paragraph: str, size: int, overlap: int) -> list[str]:
    """Split an over-long paragraph into overlapping windows."""
    if size <= 0:
        return [paragraph] if paragraph else []
    step = max(1, size - max(0, overlap))
    chunks = []
    start = 0
    while start < len(paragraph):
        chunks.append(paragraph[start:start + size])
        if start + size >= len(paragraph):
            break
        start += step
    return chunks


def chunk_text(text: str, size: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Paragraph-aware chunks of at most ~`size` chars, with overlap.

    Paragraphs are blank-line separated (which also
    approximates function boundaries in source code) and
    are accumulated whole where possible; an over-long
    paragraph is hard-split with overlap, so no passage
    falls between chunks.
    """
    text = (text or "").strip()
    if not text:
        return []
    if size <= 0:
        return [text]
    overlap = min(max(0, overlap), size - 1) if size > 1 else 0
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(para) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(para, size, overlap))
            continue
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _qdrant_available() -> bool:
    try:
        import qdrant_client  # noqa: F401
        return True
    except Exception:
        return False


class DocStore:
    """Document indexes, in memory by default, Qdrant when given one."""

    def __init__(self, store=None, home=None):
        # store: an optional Qdrant-like client with upsert/search
        # methods. It is used only when the library imports; a
        # missing library or an unsuitable object falls back to
        # memory, so ingestion never fails on the store alone.
        # home: when given, the in-memory index is also persisted
        # as JSON under <home>/docstore/, so a later process (the
        # CLI, a new run) sees what an earlier one ingested.
        # Without home the index lives only in this process.
        from pathlib import Path
        self._memory: dict[str, list[dict]] = {}
        self._meta: dict[str, dict] = {}
        self.backend = "memory"
        self.store = None
        self._dir = Path(home) / "docstore" if home is not None else None
        if self._dir is not None:
            self._dir.mkdir(parents=True, exist_ok=True)
            import json
            for path in sorted(self._dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    self._memory[path.stem] = data.get("records", [])
                    self._meta[path.stem] = data.get("meta", {})
                except Exception:
                    continue
        if store is not None and _qdrant_available() \
                and hasattr(store, "upsert"):
            self.store = store
            self.backend = "qdrant"

    def ingest(self, source_name: str, text: str) -> dict:
        """Chunk + index one document. Idempotent per content hash:
        re-ingesting the same content returns the cached index."""
        name = collection_for(text)
        digest = content_hash(text)
        if name in self._memory:
            return {"collection": name, "chunks": len(self._memory[name]),
                    "hash": digest, "cached": True}
        chunks = chunk_text(text)
        records = [{"source": source_name, "chunk": i, "text": chunk,
                    "doc_hash": digest} for i, chunk in enumerate(chunks)]
        self._memory[name] = records
        self._meta[name] = {"source": source_name, "hash": digest}
        if self._dir is not None:
            import json
            try:
                (self._dir / f"{name}.json").write_text(
                    json.dumps({"records": records, "meta": self._meta[name]}),
                    encoding="utf-8")
            except Exception:
                pass
        if self.backend == "qdrant":
            try:
                self._upsert_qdrant(name, records)
            except Exception:
                # Qdrant is an extra index, not the record: a failed
                # upsert must not lose the in-memory index.
                self.backend = "memory"
                self.store = None
        return {"collection": name, "chunks": len(chunks),
                "hash": digest, "cached": False}

    def _upsert_qdrant(self, name: str, records: list[dict]) -> None:
        from qdrant_client import models
        import uuid
        points = []
        for record in records:
            points.append(models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL,
                                  f"{record['doc_hash']}:{record['chunk']}")),
                vector=[0.0] * 8,
                payload=record))
        self.store.upsert(collection_name=name, points=points)

    def collections(self) -> list[str]:
        return sorted(self._memory)

    def search_all(self, query: str, k: int = 6) -> list[dict]:
        """Search every indexed document, best first.

        Substring-ranked over ingested chunks: a chunk scores
        one per query token it contains, plus a bonus for the
        whole query phrase. No vector is needed, so the search
        works on the in-memory index exactly as on Qdrant.
        """
        tokens = [t for t in _tokens(query)] if query else []
        if not tokens:
            return []
        phrase = query.lower()
        scored = []
        for collection, records in self._memory.items():
            for record in records:
                lowered = record["text"].lower()
                score = sum(1 for t in tokens if t in lowered)
                if phrase in lowered:
                    score += 2
                if score:
                    scored.append((score, collection, record))
        scored.sort(key=lambda item: (-item[0], item[1],
                                      item[2]["chunk"]))
        return [{"collection": collection, "chunk": record["chunk"],
                 "text": record["text"], "source": record["source"],
                 "score": score}
                for score, collection, record in scored[:k]]

    def drop(self, collection: str) -> bool:
        """Forget a collection completely. True if it existed."""
        if collection not in self._memory:
            return False
        del self._memory[collection]
        self._meta.pop(collection, None)
        if self._dir is not None:
            try:
                (self._dir / f"{collection}.json").unlink(missing_ok=True)
            except Exception:
                pass
        if self.store is not None:
            try:
                delete = getattr(self.store, "delete_collection", None)
                if callable(delete):
                    delete(collection)
            except Exception:
                pass
        return True


def _tokens(query: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", query.lower())
