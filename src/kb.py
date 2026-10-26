"""Knowledge base (KB) for RAG, Qdrant knowledge collection.

The user has a knowledge/ path under the Aidon home. They can
dump files there directly, or import files in the WebUI. WebUI
import duplicates the attached file into knowledge/ first, so
the file on disk and the file in the KB are the same file, not
a temporary upload that disappears. Then the file is auto-
ingested into the Qdrant knowledge collection for RAG.

Ingestion, in order:
1. Copy the file into knowledge/ if it is not already there.
2. Chunk it: text and markdown by paragraphs, max 1000 chars
   per chunk, 100 chars overlap, so a chunk is a readable unit
   for retrieval, not a whole file and not a single line.
3. Embed each chunk and upsert it to the knowledge collection,
   with payload: file name, chunk index, file hash, and text.
4. Track file hashes in a manifest, so a later auto-ingest
   only ingests new or changed files, and deletes chunks for
   files that were removed from knowledge/.

Auto-ingest is triggered on import and on scan: ``scan()``
walks knowledge/, ingests new or changed files, and prunes
chunks for deleted files. It does not re-ingest unchanged
files, and it never deletes the user's files, only their KB
chunks.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .memory import Memory

CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100


def chunk_text(text: str, size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into readable chunks, paragraph-aware."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 <= size:
            current = (current + "\n\n" + para).strip() if current else para
            continue
        if current:
            chunks.append(current)
        # A single paragraph longer than size is cut, with overlap.
        while len(para) > size:
            chunks.append(para[:size])
            para = para[size - overlap:]
        current = para
    if current:
        chunks.append(current)
    return chunks


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


class KnowledgeBase:
    def __init__(self, memory: Memory):
        self.memory = memory
        self.path = Path(memory.home) / "knowledge"
        self.path.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(memory.home) / "knowledge_manifest.json"

    def _manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=2))

    def import_file(self, source: Path) -> Path:
        """Duplicate a file into knowledge/, then ingest it."""
        source = Path(source)
        target = self.path / source.name
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        self.ingest_file(target)
        return target

    def ingest_file(self, path: Path) -> int:
        """Chunk and ingest one file. Returns chunk count."""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return 0
        # Remove old chunks for this file first, so a re-ingest
        # does not duplicate chunks.
        self._delete_chunks_for_file(path.name)
        chunks = chunk_text(text)
        digest = file_hash(path)
        for index, chunk in enumerate(chunks):
            self.memory.record("knowledge", chunk, {
                "file": path.name, "chunk_index": index,
                "file_hash": digest, "kind": "kb"})
        manifest = self._manifest()
        manifest[path.name] = digest
        self._save_manifest(manifest)
        return len(chunks)

    def _delete_chunks_for_file(self, file_name: str) -> None:
        for point in self.memory.list_points("knowledge", limit=1000):
            if point.payload.get("file") == file_name:
                self.memory.forget("knowledge", point.id)

    def scan(self) -> dict:
        """Auto-ingest: new/changed files in, deleted files out."""
        manifest = self._manifest()
        ingested = 0
        for path in sorted(self.path.iterdir()):
            if not path.is_file():
                continue
            digest = file_hash(path)
            if manifest.get(path.name) != digest:
                ingested += self.ingest_file(path)
        # Prune chunks for files that are gone from knowledge/.
        present = {p.name for p in self.path.iterdir() if p.is_file()}
        for file_name in list(manifest.keys()):
            if file_name not in present:
                self._delete_chunks_for_file(file_name)
                manifest.pop(file_name, None)
        self._save_manifest(manifest)
        return {"ingested_chunks": ingested, "files": len(present)}

    def recall(self, query: str, limit: int = 5):
        """RAG recall: retrieve KB chunks for a query."""
        return self.memory.recall("knowledge", query, limit=limit)
