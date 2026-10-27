"""Resume a run from a saved state file.

A run state is a plain dict saved as JSON under <home>/runs/<id>.json.
Resuming never re-runs completed steps blindly: resume_state()
checks the state has the keys a runner needs before handing it back,
and reports what is missing instead of failing later, mid-run.
"""

from __future__ import annotations

import json
from pathlib import Path

REQUIRED_KEYS = ("goal", "steps", "completed")


def _runs_dir(home) -> Path:
    directory = Path(home) / "runs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _safe_id(run_id: str) -> str:
    safe = "".join(c for c in str(run_id) if c.isalnum() or c in ("-", "_"))
    return safe or "run"


def save_run_state(home, run_id: str, state: dict) -> Path:
    """Save a run state, returning the file path."""
    path = _runs_dir(home) / f"{_safe_id(run_id)}.json"
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def load_run_state(home, run_id: str) -> dict | None:
    """Load a saved run state, or None when there is none / it is unreadable."""
    path = _runs_dir(home) / f"{_safe_id(run_id)}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def list_runs(home) -> list[str]:
    """Ids of saved runs, sorted."""
    directory = _runs_dir(home)
    return sorted(p.stem for p in directory.glob("*.json"))


def resume_state(home, run_id: str) -> tuple[bool, dict | None, str]:
    """Validate a saved state for resuming.

    Returns (ok, state, error). ok is False when there is no saved
    state, it is not a dict, or a required key (goal, steps,
    completed) is missing or mistyped: steps and completed must be
    lists, goal must be a non-empty string.
    """
    state = load_run_state(home, run_id)
    if state is None:
        return False, None, f"no saved run state for {run_id!r}"
    if not isinstance(state.get("goal"), str) or not state["goal"].strip():
        return False, None, "saved state has no goal"
    for key in ("steps", "completed"):
        if not isinstance(state.get(key), list):
            return False, None, f"saved state key {key!r} is not a list"
    return True, state, ""
