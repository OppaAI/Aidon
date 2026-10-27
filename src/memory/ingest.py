"""Folder ingest into the knowledge collection.

A whole folder of text files becomes searchable knowledge: each
file is chunked, each chunk is recorded as one point in the
"knowledge" collection, and a later re-ingest cleans up after
itself — chunks whose file shrank, was renamed, or was deleted
do not linger in retrieval forever.

Shape of a recorded point payload:
    doc_id: "<absolute path>:::<chunk index>"
    title: relative path inside the folder
    text: the chunk text
    kind: "folder"
    source_dir: the folder, as a string

The memory object generates point ids itself, so this module
tracks doc_id -> point id by reading existing points back and
matching on the payload, never by assuming an id scheme.
"""

from __future__ import annotations

from pathlib import Path

SKIP_DIRS = {".git", "__pycache__"}


def _iter_files(root: Path):
    """Text-file candidates under root, sorted, skipping junk dirs."""
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        out.append(path)
    return out


def _read_text(path: Path) -> str | None:
    """File text, or None when the bytes are not UTF-8 text."""
    try:
        return path.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _existing_points(memory, source_dir: str) -> dict[str, tuple[str, str]]:
    """doc_id -> (point_id, text) for this folder's knowledge points."""
    points = []
    iter_points = getattr(memory, "iter_points", None)
    if callable(iter_points):
        try:
            points = list(iter_points("knowledge"))
        except Exception:
            points = []
    if not points:
        list_points = getattr(memory, "list_points", None)
        if callable(list_points):
            try:
                points = list_points("knowledge", limit=100000)
            except TypeError:
                points = list_points("knowledge")
            except Exception:
                points = []
    existing = {}
    for point in points or []:
        payload = getattr(point, "payload", {}) or {}
        if payload.get("source_dir") != source_dir:
            continue
        doc_id = payload.get("doc_id")
        if doc_id:
            existing[doc_id] = (getattr(point, "id", ""), getattr(point, "text", ""))
    return existing


def _forget(memory, point_id: str) -> bool:
    forget = getattr(memory, "forget", None)
    if callable(forget):
        try:
            return bool(forget("knowledge", point_id))
        except Exception:
            return False
    store = getattr(memory, "store", None)
    delete = getattr(store, "delete", None) if store is not None else None
    if callable(delete):
        try:
            return bool(delete("knowledge", point_id))
        except Exception:
            return False
    return False


def ingest_folder(home, folder, memory=None, dry_run: bool = False) -> dict:
    """Ingest a folder into the knowledge collection.

    Returns {"files", "chunks", "skipped", "dry_run",
    "removed_stale", "stale"}: "stale" lists doc_ids that
    should have been removed but could not be, so stale
    chunks are never silently left unreported. A dry run
    counts files and chunks, writes nothing, and removes
    nothing. Raises FileNotFoundError for a missing folder.
    """
    from .docstore import chunk_text

    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"not a folder: {folder}")
    if memory is None:
        from .memory import Memory

        memory = Memory(Path(home))
    existing = _existing_points(memory, str(root))
    files = chunks = 0
    skipped: list[str] = []
    written: set[str] = set()
    for path in _iter_files(root):
        rel = str(path.relative_to(root))
        text = _read_text(path)
        if text is None:
            skipped.append(rel)
            continue
        pieces = chunk_text(text)
        files += 1
        chunks += len(pieces)
        if dry_run:
            continue
        for index, piece in enumerate(pieces):
            doc_id = f"{path}:::{index}"
            written.add(doc_id)
            old = existing.get(doc_id)
            if old is not None and old[1] == piece:
                continue
            if old is not None:
                _forget(memory, old[0])
            memory.record("knowledge", piece, {
                "doc_id": doc_id, "title": rel, "text": piece,
                "kind": "folder", "source_dir": str(root)})
    removed_stale = 0
    stale: list[str] = []
    if not dry_run:
        for doc_id, (point_id, _text) in existing.items():
            if doc_id in written:
                continue
            if _forget(memory, point_id):
                removed_stale += 1
            else:
                stale.append(doc_id)
    return {"files": files, "chunks": chunks, "skipped": skipped,
            "dry_run": bool(dry_run), "removed_stale": removed_stale,
            "stale": stale}


def remove_folder(home, folder, memory=None) -> int:
    """Delete every knowledge point ingested from a folder.

    Returns the count removed. Points from other folders
    are untouched.
    """
    root = Path(folder).expanduser().resolve()
    if memory is None:
        from .memory import Memory

        memory = Memory(Path(home))
    existing = _existing_points(memory, str(root))
    removed = 0
    for _doc_id, (point_id, _text) in existing.items():
        if _forget(memory, point_id):
            removed += 1
    return removed
