"""File conflict detection and multifile versioning.

Every write goes through a ledger (<workdir>/.versions.json) that
records each file's current version and content hash. Writing a
file with different content archives the old content as
<name>.v<N> and bumps the version; writing identical content is
a no-op that returns the current version. A write that supplies
the hash the caller last read, and finds the file changed since,
is a conflict: nothing is overwritten, and the caller gets
{conflict: True, existing, incoming} to resolve.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def _ledger_path(workdir: Path) -> Path:
    return Path(workdir) / ".versions.json"


def _load_ledger(workdir: Path) -> dict:
    path = _ledger_path(workdir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_ledger(workdir: Path, ledger: dict) -> None:
    _ledger_path(workdir).write_text(json.dumps(ledger, indent=2),
                                     encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_")


def write_versioned(workdir, name: str, text: str,
                    expected_hash: str | None = None):
    """Write a versioned file.

    Returns the new version number on success. Returns
    {"conflict": True, "existing": ..., "incoming": ...} when
    expected_hash is given and the file changed since that hash
    (and the new text differs from the current content) — the
    file is left untouched in that case.
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    target = workdir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    ledger = _load_ledger(workdir)
    entry = ledger.get(name, {"version": 0, "hash": ""})
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    current_hash = _hash(current) if target.is_file() else ""
    if expected_hash is not None and target.is_file() \
            and current_hash != expected_hash and current_hash != _hash(text):
        return {"conflict": True, "existing": current, "incoming": text}
    if target.is_file() and current == text:
        return entry["version"] or 1
    new_version = (entry["version"] or 0) + 1
    if target.is_file():
        # Archive the outgoing content under its old version number.
        old_version = entry["version"] or 1
        archive = workdir / f"{name}.v{old_version}"
        archive.write_text(current, encoding="utf-8")
    else:
        new_version = 1
    target.write_text(text, encoding="utf-8")
    ledger[name] = {"version": new_version, "hash": _hash(text)}
    _save_ledger(workdir, ledger)
    return new_version


def read_version(workdir, name: str, version: int | None = None):
    """Read a file's current content, or one archived version.

    version=None reads the current file. An archived version reads
    <name>.v<version>; asking for the current version number reads
    the current file. Returns None when the file or version is missing.
    """
    workdir = Path(workdir)
    target = workdir / name
    if version is None:
        return target.read_text(encoding="utf-8") if target.is_file() else None
    ledger = _load_ledger(workdir)
    entry = ledger.get(name, {})
    if version == entry.get("version") and target.is_file():
        return target.read_text(encoding="utf-8")
    archive = workdir / f"{name}.v{version}"
    return archive.read_text(encoding="utf-8") if archive.is_file() else None


def list_versions(workdir, name: str) -> list[int]:
    """Version numbers available for a file, oldest first."""
    workdir = Path(workdir)
    ledger = _load_ledger(workdir)
    entry = ledger.get(name, {})
    versions = set()
    if entry.get("version"):
        versions.add(entry["version"])
    for path in workdir.glob(f"{name}.v*"):
        suffix = path.name[len(name) + 2:]
        if suffix.isdigit():
            versions.add(int(suffix))
    return sorted(versions)


def current_hash(workdir, name: str) -> str | None:
    """Content hash of a file's current version, for conflict checks."""
    target = Path(workdir) / name
    if not target.is_file():
        return None
    return _hash(target.read_text(encoding="utf-8"))
