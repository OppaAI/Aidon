"""Memory extras: rolling summary, redaction, backup, export.

- Rolling summary: after a run, a short model-free summary
  (goal, files delivered, status) is appended to
  <home>/summaries/<project>.md, so a later run can read
  one file instead of re-reading every trace. Model-written
  summaries are still a climb for Aidon; this summary is
  code-written, from the run report, never invented.
- Redaction: email addresses, API-key-like tokens, and
  bearer tokens are replaced with [redacted] before a
  summary, export, or backup is written. A summary must
  never carry a secret out of the home.
- Backup and export: backup copies specs, summaries,
  learned runs, library manifest, and email drafts, never
  .env, never v1 keys, never qdrant data. Export writes
  memory points as JSONL, redacted, for the owner to move
  or inspect, not for a model to re-ingest blindly.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_TOKEN_RE = re.compile(r"(aidon_[A-Za-z0-9_-]{16,}|sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]+)")


def redact(text: str) -> str:
    text = _EMAIL_RE.sub("[redacted email]", text)
    text = _TOKEN_RE.sub("[redacted token]", text)
    return text


def rolling_summary(home: Path, project_id: str, goal: str,
                    files: list[str], status: str) -> Path:
    """Append a redacted, code-written run summary, return its path."""
    summaries = Path(home) / "summaries"
    summaries.mkdir(parents=True, exist_ok=True)
    path = summaries / f"{project_id}.md"
    line = f"- {redact(goal)} -> {status}, files: {', '.join(files) or 'none'}\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return path


def backup(home: Path, dest: Path) -> list[str]:
    """Copy non-secret home files to dest, return copied relative paths."""
    home, dest = Path(home), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for name in ("specs", "summaries", "learned", "library_manifest.json",
                 "scheduler.json", "email"):
        source = home / name
        if not source.exists():
            continue
        target = dest / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
            for path in sorted(target.rglob("*")):
                if path.is_file():
                    copied.append(str(path.relative_to(dest)))
        else:
            shutil.copy2(source, target)
            copied.append(name)
    return copied


def export_points(memory, dest: Path) -> int:
    """Write memory points as redacted JSONL, return the count."""
    count = 0
    with Path(dest).open("w", encoding="utf-8") as handle:
        for collection in ("working", "episodic", "semantic", "experience", "knowledge"):
            try:
                points = memory.list_points(collection, limit=1000)
            except Exception:
                continue
            for point in points:
                handle.write(json.dumps({
                    "collection": collection, "id": point.id,
                    "text": redact(point.text),
                    "payload": {k: redact(str(v)) for k, v in point.payload.items()}}) + "\n")
                count += 1
    return count
