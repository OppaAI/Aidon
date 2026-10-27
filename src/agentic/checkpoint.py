"""Crash-resume checkpoints for a goal run.

Persists enough state that a restarted process can skip sandbox work
already completed for the same run_id, without replaying side effects.

Storage: Registry.run_state (SQLite under <home>/registry.db), key
``goal_checkpoint``. JSON blob, one per run_id.

Safety rules (deliberate):
- Live / approve is never auto-skipped. A resumed run that already
  verified still asks the owner again before live actions.
- Checkpoint is cleared on terminal status (done / failed / rejected).
- Missing or corrupt checkpoints are ignored; the run starts fresh.
- Deliverable skip only applies when the file still exists in the
  sandbox workdir (or the step had no file deliverable recorded).

This is durable *progress* recovery, not a full Temporal-style
workflow engine. Mission snapshots remain the review record;
checkpoints are the in-flight execution cursor.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def normalize_goal(goal: str) -> str:
    """Stable comparison form: collapse whitespace, strip."""
    return " ".join(str(goal or "").split())
  
CHECKPOINT_KEY = "goal_checkpoint"

# Phases ordered roughly through the pipeline. "live" means owner
# already approved once; on resume we still re-ask before live work.
PHASES = (
    "started",
    "sandbox",
    "verified",
    "approved",
    "live",
    "done",
    "failed",
    "rejected",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RunCheckpoint:
    run_id: str
    goal: str = ""
    phase: str = "started"
    plan: dict = field(default_factory=dict)
    passed: list[str] = field(default_factory=list)
    verified: list[str] = field(default_factory=list)
    attempt: int = 1
    updated_at: str = ""

    def to_json(self) -> str:
        data = asdict(self)
        if not data.get("updated_at"):
            data["updated_at"] = _now()
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "RunCheckpoint | None":
        if not text or not str(text).strip():
            return None
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict) or not data.get("run_id"):
            return None
        phase = str(data.get("phase") or "started")
        if phase not in PHASES:
            phase = "started"
        return cls(
            run_id=str(data["run_id"]),
            goal=str(data.get("goal") or ""),
            phase=phase,
            plan=dict(data.get("plan") or {}) if isinstance(data.get("plan"), dict) else {},
            passed=[str(x) for x in (data.get("passed") or [])],
            verified=[str(x) for x in (data.get("verified") or [])],
            attempt=max(1, int(data.get("attempt") or 1)),
            updated_at=str(data.get("updated_at") or ""),
        )


class CheckpointStore:
    """Thin wrapper over Registry.save_state / get_state / clear_state."""

    def __init__(self, registry):
        self.registry = registry

    def save(self, checkpoint: RunCheckpoint) -> None:
        checkpoint.updated_at = _now()
        self.registry.save_state(
            checkpoint.run_id, CHECKPOINT_KEY, checkpoint.to_json())

    def load(self, run_id: str) -> RunCheckpoint | None:
        if not run_id:
            return None
        raw = self.registry.get_state(run_id, CHECKPOINT_KEY)
        return RunCheckpoint.from_json(raw)
      
    def load_for_goal(self, run_id: str, goal: str) -> RunCheckpoint | None:
        """Load checkpoint only when the saved goal matches."""
        cp = self.load(run_id)
        if cp is None:
            return None
        if normalize_goal(cp.goal) != normalize_goal(goal):
            return None
        return cp
      
    def clear(self, run_id: str) -> None:
        if not run_id:
            return
        # Only drop the checkpoint key; other run_state keys (if any) stay.
        try:
            self.registry.save_state(run_id, CHECKPOINT_KEY, "")
        except Exception:
            pass


def plan_to_dict(plan: Any) -> dict:
    """Serialize a loop Plan (or dict) for the checkpoint blob."""
    if plan is None:
        return {}
    if isinstance(plan, dict):
        return {
            "steps": list(plan.get("steps") or []),
            "deliverables": list(plan.get("deliverables") or []),
            "criteria": list(plan.get("criteria") or []),
        }
    steps_out = []
    for step in getattr(plan, "steps", []) or []:
        if hasattr(step, "tool"):
            steps_out.append({
                "tool": step.tool,
                "args": dict(step.args or {}),
                "deliverable": step.deliverable or "",
                "criteria": step.criteria or "",
                "executor": getattr(step, "executor", "tool") or "tool",
            })
        elif isinstance(step, dict):
            steps_out.append(dict(step))
        else:
            steps_out.append({"tool": "write", "args": {"text": str(step)},
                              "deliverable": "deliverable.md"})
    return {
        "steps": steps_out,
        "deliverables": list(getattr(plan, "deliverables", []) or []),
        "criteria": list(getattr(plan, "criteria", []) or []),
    }


def deliverable_still_present(workdir: Path, name: str) -> bool:
    """True when a named deliverable file still exists under workdir.

    Empty names (non-file steps) count as present so they can stay in
    the passed set without blocking resume.
    """
    if not name or not str(name).strip():
        return True
    path = Path(workdir) / str(name)
    try:
        path = path.resolve()
        root = Path(workdir).resolve()
        if path != root and root not in path.parents:
            return False
        return path.is_file()
    except OSError:
        return False


def filter_resumable_passed(
    passed: list[str], workdir: Path,
) -> list[str]:
    """Keep only passed deliverables that still exist on disk."""
    return [name for name in passed
            if deliverable_still_present(workdir, name)]
Grok finished responding
