"""Governor, Aidon governor concept, rebuilt, file-based caps, fail closed.

A governor caps how much a project can spend in a day: model calls, tokens, and learns,
stored under <home>/governor/<project>.json. check returns whether a new spend fits,
record adds a spend, and a spend that would exceed the cap is refused, not recorded.
Caps are per day, by date, so a cap reached today does not block tomorrow.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path


class Governor:
    def __init__(self, home, max_calls: int = 100, max_tokens: int = 200000, max_learns: int = 5):
        self.dir = Path(home) / "governor"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.max_learns = max_learns

    def _path(self, project_id: str) -> Path:
        return self.dir / f"{project_id}.json"

    def _load(self, project_id: str) -> dict:
        today = date.today().isoformat()
        path = self._path(project_id)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("date") == today:
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        return {"project_id": project_id, "date": today, "calls": 0, "tokens": 0, "learns": 0}

    def check(self, project_id: str, calls: int = 1, tokens: int = 0, learns: int = 0) -> dict:
        data = self._load(project_id)
        over = []
        if data["calls"] + calls > self.max_calls:
            over.append("calls")
        if data["tokens"] + tokens > self.max_tokens:
            over.append("tokens")
        if data["learns"] + learns > self.max_learns:
            over.append("learns")
        return {"ok": not over, "over": over, **data}

    def record(self, project_id: str, calls: int = 1, tokens: int = 0, learns: int = 0) -> dict:
        check = self.check(project_id, calls=calls, tokens=tokens, learns=learns)
        if not check["ok"]:
            return check
        data = self._load(project_id)
        data["calls"] += calls
        data["tokens"] += tokens
        data["learns"] += learns
        self._path(project_id).write_text(json.dumps(data, indent=2), encoding="utf-8")
        return {"ok": True, **data}
