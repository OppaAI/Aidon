"""Steer and cancel a running run, from any surface, same file, same run.

A run that started in the WebUI, on WhatsApp, on Slack, or
from the CLI, must be steerable and stoppable from any of
those surfaces, while it is running, not only before it starts.
That only works if control state is shared, not per-process:
a CLI control command runs in a different process from a
WebUI run, so in-memory state alone cannot stop it.

RunControl is file-based, in the Aidon home, under runs/:
- runs/<run_id>.json, with goal, conversation_id, status,
  steer_notes, and cancelled.
- register(run_id, goal, conversation_id) creates the file.
- cancel(run_id) sets cancelled=true, and the running loop
  checks it before every step and between attempts, and
  returns a cancelled report, without executing further steps.
- steer(run_id, note) appends a note, and the running loop
  applies steer notes to the next repair attempt, as repair
  context, so a steer does not rewrite passed work, it
  changes what the next attempt does.
- A run that is waiting on an approval card is cancelled
  the same way: cancel also resolves pending WebUI cards
  as rejected, via cancel_all on the gate, when the caller
  passes a gate, so a cancelled run does not sit on a card
  until its 180 second timeout.

Run ids are short, printed by the CLI and in the WebUI
reply, and used by every surface: "cancel <run_id>" on
WhatsApp or Slack, POST /api/cancel in the WebUI, and
"aidon control cancel <run_id>" in the CLI, all write the
same file, and the loop reads the same file.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def new_run_id() -> str:
    return uuid.uuid4().hex[:8]


class RunControl:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.runs_dir = self.home / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> Path:
        # A run id is a short hex id, never a path, so a
        # crafted id cannot escape runs/.
        safe = "".join(c for c in run_id if c.isalnum() or c in ("-", "_"))[:32]
        return self.runs_dir / f"{safe}.json"

    def register(self, run_id: str, goal: str, conversation_id: str = "") -> dict:
        record = {"run_id": run_id, "goal": goal, "conversation_id": conversation_id,
                  "status": "running", "steer_notes": [], "cancelled": False,
                  "created_at": datetime.now(timezone.utc).isoformat()}
        self._path(run_id).write_text(json.dumps(record, indent=2))
        return record

    def get(self, run_id: str) -> dict | None:
        path = self._path(run_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def list_runs(self) -> list[dict]:
        out = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                out.append(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return out

    def cancel(self, run_id: str, gate=None) -> bool:
        record = self.get(run_id)
        if record is None:
            return False
        record["cancelled"] = True
        record["status"] = "cancelled"
        self._path(run_id).write_text(json.dumps(record, indent=2))
        if gate is not None and hasattr(gate, "cancel_all"):
            gate.cancel_all()
        return True

    def steer(self, run_id: str, note: str) -> bool:
        record = self.get(run_id)
        if record is None:
            return False
        record.setdefault("steer_notes", []).append(note)
        self._path(run_id).write_text(json.dumps(record, indent=2))
        return True

    def set_status(self, run_id: str, status: str) -> bool:
        """Record the terminal/current status of a run."""
        record = self.get(run_id)
        if record is None:
            return False
        record["status"] = str(status)
        if status in ("done", "failed", "rejected", "cancelled"):
            record["stage"] = "done"
        self._path(run_id).write_text(json.dumps(record, indent=2))
        return True

    def set_stage(self, run_id: str, stage: str) -> bool:
        """Publish the current pipeline stage for the WebUI progress bar."""
        record = self.get(run_id)
        if record is None:
            return False
        record["stage"] = str(stage)
        self._path(run_id).write_text(json.dumps(record, indent=2))
        return True

    def is_cancelled(self, run_id: str | None) -> bool:
        if not run_id:
            return False
        record = self.get(run_id)
        return bool(record and record.get("cancelled"))

    def steer_notes(self, run_id: str | None) -> list[str]:
        if not run_id:
            return []
        record = self.get(run_id)
        return list(record.get("steer_notes", [])) if record else []
