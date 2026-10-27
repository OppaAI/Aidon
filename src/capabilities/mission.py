"""Mission lifecycle with frozen snapshots, stdlib only.

A mission starts as a draft: a project id plus a spec dict.
When it is ready for review, freeze() copies the spec and
its deliverables into a snapshot file, stamps it with a
timestamp and a sha256 hash, and moves the mission to
ready_for_review. The snapshot is the frozen record of
what was approved: a second freeze does not overwrite it,
unless force=True, so a reviewed snapshot cannot be
silently replaced by a later edit. The hash covers only
the spec and deliverables, in canonical JSON form, so it
is stable across freezes: the same spec and deliverables
always give the same hash, even if the timestamp differs.
A frozen mission can be unfrozen back to in_progress,
for example when review asks for changes, without
deleting the snapshot that was reviewed.

Storage, all under the Aidon home, never outside it:
  <home>/missions/<project_id>/mission.json
  <home>/missions/<project_id>/snapshot.json
No secrets are stored here; specs are the caller's data.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("draft", "ready_for_review", "in_progress", "done", "cancelled")


def _mission_dir(home: Path, project_id: str) -> Path:
    return Path(home) / "missions" / project_id


def _load_mission(home: Path, project_id: str) -> dict:
    path = _mission_dir(home, project_id) / "mission.json"
    if not path.is_file():
        raise FileNotFoundError(f"no mission for project {project_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _save_mission(home: Path, mission: dict) -> None:
    directory = _mission_dir(home, mission["project_id"])
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mission.json").write_text(
        json.dumps(mission, indent=2, sort_keys=True), encoding="utf-8")


def _snapshot_hash(spec: dict, deliverables: list) -> str:
    canonical = json.dumps(
        {"spec": spec, "deliverables": deliverables},
        sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class MissionStore:
    """Create, freeze, unfreeze, and track missions."""

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)

    def create(self, project_id: str, spec: dict) -> dict:
        if not project_id:
            raise ValueError("project_id is required")
        if not isinstance(spec, dict):
            raise ValueError("spec must be a dict")
        spec_copy = copy.deepcopy(spec)
        deliverables = copy.deepcopy(spec_copy.get("deliverables", []))
        mission = {
            "project_id": project_id,
            "spec": spec_copy,
            "deliverables": deliverables,
            "status": "draft",
        }
        _save_mission(self.home, mission)
        return copy.deepcopy(mission)

    def get(self, project_id: str) -> dict:
        return _load_mission(self.home, project_id)

    def status(self, project_id: str) -> str:
        return _load_mission(self.home, project_id)["status"]

    def set_status(self, project_id: str, status: str) -> dict:
        if status not in STATUSES:
            raise ValueError(
                f"status must be one of {', '.join(STATUSES)}, not {status}")
        mission = _load_mission(self.home, project_id)
        mission["status"] = status
        _save_mission(self.home, mission)
        return copy.deepcopy(mission)

    def freeze(self, project_id: str, force: bool = False) -> dict:
        mission = _load_mission(self.home, project_id)
        directory = _mission_dir(self.home, project_id)
        snapshot_path = directory / "snapshot.json"
        if snapshot_path.is_file() and not force:
            return json.loads(snapshot_path.read_text(encoding="utf-8"))
        spec = copy.deepcopy(mission["spec"])
        deliverables = copy.deepcopy(mission.get("deliverables", []))
        snapshot = {
            "project_id": project_id,
            "spec": spec,
            "deliverables": deliverables,
            "status": "ready_for_review",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sha256": _snapshot_hash(spec, deliverables),
        }
        directory.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
        mission["status"] = "ready_for_review"
        _save_mission(self.home, mission)
        return copy.deepcopy(snapshot)

    def unfreeze(self, project_id: str) -> dict:
        mission = _load_mission(self.home, project_id)
        mission["status"] = "in_progress"
        _save_mission(self.home, mission)
        return copy.deepcopy(mission)
