"""Lifecycle, old Aidon lifecycle concept, rebuilt, separate from mission snapshots.

A project lifecycle is a state machine, file-based under <home>/lifecycle/<project>.json:
draft -> ready_for_review -> in_progress -> done, with cancelled from any state, and
rejected back to draft from ready_for_review. Invalid transitions are refused, not
silently applied, so a project cannot jump from draft to done without review.
"""

from __future__ import annotations

import json
from pathlib import Path

TRANSITIONS = {
    "draft": ("ready_for_review", "cancelled"),
    "ready_for_review": ("in_progress", "draft", "cancelled"),
    "in_progress": ("done", "cancelled"),
    "done": (),
    "cancelled": (),
}


class LifecycleStore:
    def __init__(self, home):
        self.dir = Path(home) / "lifecycle"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        return self.dir / f"{project_id}.json"

    def create(self, project_id: str) -> dict:
        data = {"project_id": project_id, "status": "draft"}
        self._path(project_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def get(self, project_id: str) -> dict | None:
        path = self._path(project_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def transition(self, project_id: str, new_status: str) -> dict:
        current = self.get(project_id)
        if current is None:
            return {"ok": False, "error": f"no lifecycle for {project_id}"}
        allowed = TRANSITIONS.get(current["status"], ())
        if new_status not in allowed:
            return {"ok": False, "error": f"invalid transition {current['status']} -> {new_status}, allowed: {', '.join(allowed)}"}
        current["status"] = new_status
        self._path(project_id).write_text(json.dumps(current, indent=2), encoding="utf-8")
        return {"ok": True, **current}
