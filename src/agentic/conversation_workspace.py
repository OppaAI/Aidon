"""Verified artifacts and workflow context retained per conversation.

Every run still gets a fresh sandbox. Only files reported as verified on a
successful run are copied into this store, and only into later runs in the
same conversation. This keeps cross-run work useful without sharing a live
work directory between concurrent or unrelated goals.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path, PurePosixPath

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _safe_relative(name: str) -> PurePosixPath | None:
    value = str(name or "").replace("\\", "/")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def _workflow_record(workflow: dict | None) -> dict:
    workflow = workflow if isinstance(workflow, dict) else {}
    steps = workflow.get("steps")
    return {
        "goal": str(workflow.get("goal", ""))[:4000],
        "summary": str(workflow.get("summary", ""))[:2000],
        "criteria": [str(item)[:500] for item in workflow.get("criteria", [])[:30]]
        if isinstance(workflow.get("criteria"), list) else [],
        "steps": [
            {"tool": str(step.get("tool", ""))[:100],
             "description": str(step.get("description", ""))[:500]}
            for step in steps[:30] if isinstance(step, dict)
        ] if isinstance(steps, list) else [],
    }


class ConversationWorkspace:
    """Carry verified files and a compact workflow note across turns."""

    def __init__(self, home, conversation_id: str):
        self.home = Path(home)
        digest = hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()
        self.root = self.home / "conversation_workspaces" / digest
        self.files = self.root / "files"
        self.manifest = self.root / "manifest.json"
        key = str(self.manifest.resolve())
        with _LOCKS_GUARD:
            self._lock = _LOCKS.setdefault(key, threading.RLock())

    def _load(self) -> dict:
        try:
            data = json.loads(self.manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def stage(self, workdir) -> dict:
        """Copy saved verified artifacts into a fresh run directory."""
        target_root = Path(workdir).resolve()
        staged: list[str] = []
        with self._lock:
            data = self._load()
            for name in data.get("artifacts", []):
                relative = _safe_relative(name)
                if relative is None:
                    continue
                source = self.files.joinpath(*relative.parts)
                try:
                    resolved = source.resolve(strict=True)
                    if not resolved.is_file() or not resolved.is_relative_to(self.files.resolve()):
                        continue
                    destination = target_root.joinpath(*relative.parts)
                    if not destination.resolve().is_relative_to(target_root):
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(resolved, destination)
                    staged.append(relative.as_posix())
                except OSError:
                    continue
            workflow = data.get("workflow", {})
        return {"artifacts": staged,
                "workflow": workflow if isinstance(workflow, dict) else {}}

    def publish(self, workdir, verified_files: list[str], workflow: dict | None = None) -> list[str]:
        """Persist only explicitly verified files from a completed run."""
        source_root = Path(workdir).resolve()
        published: list[str] = []
        with self._lock:
            self.files.mkdir(parents=True, exist_ok=True)
            data = self._load()
            artifacts = {str(name) for name in data.get("artifacts", [])
                         if _safe_relative(str(name)) is not None}
            for name in verified_files or []:
                raw = Path(str(name))
                source = raw if raw.is_absolute() else source_root / raw
                try:
                    resolved = source.resolve(strict=True)
                    relative = resolved.relative_to(source_root)
                except (OSError, ValueError):
                    continue
                safe = _safe_relative(relative.as_posix())
                if safe is None or not resolved.is_file():
                    continue
                destination = self.files.joinpath(*safe.parts)
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    fd, temporary = tempfile.mkstemp(
                        prefix=".artifact-", dir=destination.parent)
                    os.close(fd)
                    try:
                        shutil.copyfile(resolved, temporary)
                        os.replace(temporary, destination)
                    finally:
                        try:
                            os.unlink(temporary)
                        except FileNotFoundError:
                            pass
                except OSError:
                    continue
                artifacts.add(safe.as_posix())
                published.append(safe.as_posix())

            record = {
                "artifacts": sorted(artifacts),
                "workflow": _workflow_record(workflow),
            }
            self.root.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".manifest-", dir=self.root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(record, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.manifest)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        return published
