"""Artifact inventory and goal files.

The inventory is a code-only listing of what a workdir actually
contains: path (relative), size in bytes, and kind by suffix.
Goal files are the subset whose name or content mentions the
goal's keywords, so a run can answer "what did this goal
produce?" from files, not from memory.
"""

from __future__ import annotations

import re
from pathlib import Path

_KIND_BY_SUFFIX = {
    ".md": "text", ".txt": "text", ".rst": "text",
    ".py": "code", ".js": "code", ".ts": "code", ".sh": "code",
    ".json": "data", ".csv": "data", ".yaml": "data", ".yml": "data",
    ".png": "image", ".jpg": "image", ".jpeg": "image",
    ".svg": "image", ".gif": "image",
    ".pdf": "document", ".docx": "document", ".epub": "document",
}

_STOPWORDS = {"the", "and", "for", "with", "that", "this", "from",
              "into", "your", "you", "are", "our", "plan", "make",
              "build", "create", "write"}


def _kind(path: Path) -> str:
    return _KIND_BY_SUFFIX.get(path.suffix.lower(), "file")


def inventory(workdir) -> list[dict]:
    """All non-hidden files in a workdir, sorted by path."""
    workdir = Path(workdir)
    rows = []
    if not workdir.is_dir():
        return rows
    for path in sorted(p for p in workdir.rglob("*") if p.is_file()):
        rel = path.relative_to(workdir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        rows.append({"path": str(rel), "size": path.stat().st_size,
                     "kind": _kind(path)})
    return rows


def _keywords(goal: str) -> set[str]:
    words = set(re.findall(r"[a-z0-9]+", (goal or "").lower()))
    return {w for w in words if len(w) >= 4 and w not in _STOPWORDS}


def goal_files(workdir, goal: str) -> list[dict]:
    """Inventory rows whose name or text content mentions a goal keyword."""
    keywords = _keywords(goal)
    if not keywords:
        return []
    rows = []
    for row in inventory(workdir):
        path = Path(workdir) / row["path"]
        hay = row["path"].lower()
        try:
            hay += " " + path.read_text(encoding="utf-8", errors="ignore").lower()
        except OSError:
            pass
        if any(keyword in hay for keyword in keywords):
            rows.append(row)
    return rows


def inventory_markdown(workdir) -> str:
    """The inventory as a Markdown table, with a total row."""
    rows = inventory(workdir)
    lines = ["| path | size | kind |", "| --- | ---: | --- |"]
    total = 0
    for row in rows:
        lines.append(f"| {row['path']} | {row['size']} | {row['kind']} |")
        total += row["size"]
    lines.append(f"| **total ({len(rows)} files)** | **{total}** | |")
    return "\n".join(lines)
