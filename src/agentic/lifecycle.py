"""Lifecycle, Aidon lifecycle concept, rebuilt, separate from mission snapshots.

A conversation lifecycle is a state machine, file-based under <home>/lifecycle/<conversation>.json:
draft -> ready_for_review -> in_progress -> done, with cancelled from any state, and
rejected back to draft from ready_for_review. Invalid transitions are refused, not
silently applied, so a conversation cannot jump from draft to done without review.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..capabilities.mission import (InvalidTransition, grade_criteria)

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

    def _path(self, conversation_id: str) -> Path:
        return self.dir / f"{conversation_id}.json"

    def create(self, conversation_id: str) -> dict:
        data = {"conversation_id": conversation_id, "status": "draft"}
        self._path(conversation_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
        return data

    def get(self, conversation_id: str) -> dict | None:
        path = self._path(conversation_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def transition(self, conversation_id: str, new_status: str) -> dict:
        current = self.get(conversation_id)
        if current is None:
            return {"ok": False, "error": f"no lifecycle for {conversation_id}"}
        allowed = TRANSITIONS.get(current["status"], ())
        if new_status not in allowed:
            return {"ok": False, "error": f"invalid transition {current['status']} -> {new_status}, allowed: {', '.join(allowed)}"}
        current["status"] = new_status
        self._path(conversation_id).write_text(json.dumps(current, indent=2), encoding="utf-8")
        return {"ok": True, **current}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MissionLifecycle:
    """Durable, evidence-graded mission lifecycle on the registry database."""

    TRANSITIONS = {
        "researching": ("testing", "rejected"),
        "testing": ("ready_for_review", "rejected"),
        "ready_for_review": ("approved", "rejected"),
        "approved": ("promoted",),
        "rejected": (), "promoted": (), "discarded": (),
    }

    def __init__(self, registry):
        self.registry = registry

    def start(self, goal: str, run_id: str, workspace: str = "",
              guidance: str = "") -> dict:
        mission_id = uuid.uuid4().hex[:8]
        now = _now()
        mission = {
            "id": mission_id, "run_id": run_id, "goal": goal,
            "state": "researching", "workspace": workspace,
            "snapshot_ref": "", "snapshot_json": "", "report": "",
            "history_json": json.dumps([{
                "from": None, "to": "researching", "at": now,
                "evidence": "mission started" +
                (f"; user guidance: {guidance[:500]}" if guidance.strip() else "")
            }]), "created_at": now, "updated_at": now,
        }
        self.registry.save_mission(mission)
        return self.get(mission_id)

    def get(self, mission_id: str) -> dict | None:
        return self.registry.get_mission(mission_id)

    def list(self, limit: int = 20) -> list[dict]:
        return self.registry.list_missions(limit=limit)

    def transition(self, mission_id: str, to_state: str,
                   evidence: str = "") -> dict:
        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(f"no mission {mission_id!r}")
        if to_state not in self.TRANSITIONS.get(mission["state"], ()):
            raise InvalidTransition(
                f"cannot transition mission {mission_id} from {mission['state']!r}")
        history = json.loads(mission.get("history_json") or "[]")
        history.append({"from": mission["state"], "to": to_state,
                        "at": _now(), "evidence": evidence[:500]})
        mission["state"] = to_state
        mission["history_json"] = json.dumps(history)
        mission["updated_at"] = _now()
        self.registry.save_mission(mission)
        return mission

    def set_report(self, mission_id: str, report: str) -> dict:
        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(f"no mission {mission_id!r}")
        mission["report"] = report
        mission["updated_at"] = _now()
        self.registry.save_mission(mission)
        return mission

    def freeze_snapshot(self, mission_id: str, changes: list[dict],
                        test_results: dict, artifacts: list[str],
                        criteria: list[dict]) -> str:
        mission = self.get(mission_id)
        if mission is None:
            raise KeyError(f"no mission {mission_id!r}")
        if mission["state"] != "testing":
            raise InvalidTransition("can only freeze a snapshot from testing")
        if mission.get("snapshot_json"):
            raise InvalidTransition("snapshot already frozen")
        snapshot = {"mission_id": mission_id, "frozen_at": _now(),
                    "changes": changes or [], "test_results": test_results or {},
                    "artifacts": artifacts or [], "criteria": grade_criteria(criteria)}
        canonical = json.dumps(snapshot, sort_keys=True)
        ref = hashlib.sha256(canonical.encode()).hexdigest()[:16]
        mission["snapshot_ref"] = ref
        mission["snapshot_json"] = canonical
        self.registry.save_mission(mission)
        self.transition(mission_id, "ready_for_review", evidence=f"snapshot {ref} frozen")
        return ref

    def get_snapshot(self, mission_id: str) -> dict | None:
        mission = self.get(mission_id)
        if mission is None or not mission.get("snapshot_json"):
            return None
        return json.loads(mission["snapshot_json"])

    def verify_snapshot(self, mission_id: str) -> bool:
        mission = self.get(mission_id)
        if mission is None or not mission.get("snapshot_json"):
            return False
        return hashlib.sha256(mission["snapshot_json"].encode()).hexdigest()[:16] == mission["snapshot_ref"]

    def discard(self, mission_id: str) -> bool:
        mission = self.get(mission_id)
        if mission is None:
            return False
        if mission["state"] != "rejected":
            raise InvalidTransition("can only discard a rejected mission")
        workspace = mission.get("workspace")
        self.registry.delete_mission(mission_id)
        if workspace:
            import shutil
            shutil.rmtree(workspace, ignore_errors=True)
        return True

    def review_record(self, mission_id: str) -> dict | None:
        mission = self.get(mission_id)
        if mission is None:
            return None
        return {"mission_id": mission["id"], "run_id": mission.get("run_id", ""),
                "goal": mission["goal"], "state": mission["state"],
                "workspace": mission.get("workspace", ""),
                "snapshot_ref": mission.get("snapshot_ref", ""),
                "snapshot": self.get_snapshot(mission_id),
                "report": mission.get("report", ""),
                "history": json.loads(mission.get("history_json") or "[]"),
                "created_at": mission.get("created_at", ""),
                "updated_at": mission.get("updated_at", "")}


class NotificationOutbox:
    """Durable registry-backed notifications with spaced retries."""

    def __init__(self, registry, max_attempts: int = 5):
        self.registry = registry
        self.max_attempts = max_attempts

    def enqueue(self, kind: str, title: str, body: str = "", channel: str = "") -> int:
        return self.registry.outbox_enqueue(kind, title, body, channel)

    def pending(self) -> list[dict]:
        return self.registry.outbox_pending()

    def drain(self, sender) -> dict:
        delivered, failed, dead = 0, [], []
        for item in self.pending():
            try:
                result = sender(item)
                if result is False or (isinstance(result, dict) and result.get("ok") is False):
                    raise RuntimeError("notification sender reported failure")
            except Exception as exc:
                failed.append(item["id"])
                attempts = int(item.get("attempts") or 0) + 1
                status = "dead" if attempts >= self.max_attempts else "pending"
                self.registry.outbox_mark(item["id"], status,
                                          f"{type(exc).__name__}: {exc}"[:500])
                if status == "dead":
                    dead.append(item["id"])
                continue
            self.registry.outbox_mark(item["id"], "delivered")
            delivered += 1
        return {"delivered": delivered, "failed": failed, "dead": dead}


