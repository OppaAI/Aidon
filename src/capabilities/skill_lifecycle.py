"""Skill and plugin lifecycle, plus a governor, stdlib only.

A skill does not stay new forever. It starts as a
candidate, becomes validated after 1 success, becomes
trusted after 5 successes, and retires after 3 failures.
Retirement is sticky: a retired skill is not revived by
a later success, it must be deliberately re-added. This
keeps a flaky skill from being trusted on the strength
of one good run after three bad ones.

The governor is separate from trust. Even a trusted
skill is capped on how many times it may be learned
per day, so a runaway loop cannot keep adding skills.
governor_check() returns False when the skill is retired
or when today's learn count has reached the cap, and
True otherwise. Learns are counted by calendar date,
in the store, so the cap resets the next day.

Storage: one JSON file, <home>/skill_lifecycle.json,
with per-skill state, success and failure counts, and
per-date learn counts. No secrets are stored here.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

STATES = ("candidate", "validated", "trusted", "retired")
_VALIDATED_AFTER = 1
_TRUSTED_AFTER = 5
_RETIRED_AFTER = 3


class LifecycleStore:
    """Track lifecycle state and daily learn caps per skill."""

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "skill_lifecycle.json"
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")

    def _entry(self, name: str) -> dict:
        entry = self._data.get(name)
        if entry is None:
            entry = {"state": "candidate", "successes": 0,
                     "failures": 0, "learns": {}}
            self._data[name] = entry
        return entry

    def state(self, name: str) -> str:
        entry = self._data.get(name)
        return entry["state"] if entry else "candidate"

    def record_success(self, name: str) -> str:
        entry = self._entry(name)
        if entry["state"] == "retired":
            self._save()
            return "retired"
        entry["successes"] += 1
        if entry["successes"] >= _TRUSTED_AFTER:
            entry["state"] = "trusted"
        elif entry["successes"] >= _VALIDATED_AFTER:
            entry["state"] = "validated"
        self._save()
        return entry["state"]

    def record_failure(self, name: str) -> str:
        entry = self._entry(name)
        if entry["state"] == "retired":
            self._save()
            return "retired"
        entry["failures"] += 1
        try:
            retire_after = max(1, int(os.environ.get(
                "AIDON_SKILL_MAX_FAILURES", str(_RETIRED_AFTER)) or
                str(_RETIRED_AFTER)))
        except ValueError:
            retire_after = _RETIRED_AFTER
        if entry["failures"] >= retire_after:
            entry["state"] = "retired"
        self._save()
        return entry["state"]

    def record_learn(self, name: str) -> int:
        entry = self._entry(name)
        today = date.today().isoformat()
        learns = entry.setdefault("learns", {})
        learns[today] = learns.get(today, 0) + 1
        self._save()
        return learns[today]

    def governor_check(self, name: str, max_learns_per_day: int = 5) -> bool:
        if self.state(name) == "retired":
            return False
        entry = self._data.get(name)
        if not entry:
            return True
        today = date.today().isoformat()
        return entry.get("learns", {}).get(today, 0) < max_learns_per_day
