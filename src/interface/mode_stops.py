"""Stopped Ask/Plan runs: the proceed record.

An Ask run stops with a spec; a Plan run stops with a plan. The run
itself ends there (no blocked thread, no approval timeout), and the
record below is what a later proceed resumes from. One JSON file
under the Aidon home, the same pattern as the think flags: stop id
to record. A record is deleted when its proceed run finishes done,
so a failed proceed can be retried; a stale record is inert (it is
only read by an explicit proceed).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

_STOPS_FILE = "mode_stops.json"
_MODES = ("ask", "plan")


def _stops_path(home: Path) -> Path:
    return Path(home) / _STOPS_FILE


def _load(home: Path) -> dict:
    try:
        data = json.loads(_stops_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save(home: Path, stops: dict) -> None:
    path = _stops_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(stops, indent=2), encoding="utf-8")
    tmp.replace(path)


def save_mode_stop(home: Path, mode: str, goal: str, spec=None, plan=None,
                   conversation_key: str = "",
                   conversation_id: str = "") -> str:
    """Save a stopped run; returns its stop id."""
    if mode not in _MODES:
        raise ValueError(f"unknown stop mode: {mode}")
    stops = _load(home)
    stop_id = uuid.uuid4().hex[:12]
    stops[stop_id] = {
        "stop_id": stop_id,
        "mode": mode,
        "goal": str(goal or ""),
        "spec": spec if isinstance(spec, dict) else None,
        "plan": plan if isinstance(plan, dict) else None,
        "conversation_key": str(conversation_key or ""),
        "conversation_id": str(conversation_id or ""),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _save(home, stops)
    return stop_id


def load_mode_stop(home: Path, stop_id: str) -> dict | None:
    """Return one stop record by id, or None."""
    if not stop_id:
        return None
    record = _load(home).get(str(stop_id))
    return dict(record) if isinstance(record, dict) else None


def enrich_mode_stop(home: Path, mode_stop: dict) -> dict:
    """Attach the stored spec/plan to a run's mode_stop for the frontend.

    The loop's mode_stop carries only stop_id/mode/goal; the frozen spec
    (Ask) or plan (Plan) lives in the stop record. The Proceed card's
    View spec/plan button renders from these fields. Unknown ids pass
    through unchanged.
    """
    out = dict(mode_stop or {})
    record = load_mode_stop(home, str(out.get("stop_id") or ""))
    if record is None:
        return out
    if isinstance(record.get("spec"), dict):
        out["spec"] = record["spec"]
    if isinstance(record.get("plan"), dict):
        out["plan"] = record["plan"]
    return out


def delete_mode_stop(home: Path, stop_id: str) -> bool:
    """Delete a stop record; True when one was there."""
    stops = _load(home)
    if str(stop_id) not in stops:
        return False
    del stops[str(stop_id)]
    _save(home, stops)
    return True


def latest_mode_stop(home: Path, conversation_key: str) -> dict | None:
    """Newest stop for a conversation key, or None."""
    key = str(conversation_key or "")
    candidates = [r for r in _load(home).values()
                  if isinstance(r, dict)
                  and r.get("conversation_key") == key]
    if not candidates:
        return None
    candidates.sort(key=lambda r: str(r.get("created_at", "")))
    return dict(candidates[-1])
