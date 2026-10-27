"""Conversations, old Aidon conversations concept, rebuilt as a separate module.

A conversation is a file under <home>/conversations/<id>.json, with turns, role, text, and time.
This is separate from Memory short-term memory: it is the durable transcript of one chat,
so a later run can resume it, summarise it, or forget it as a whole, without touching other
conversations. No model call here, recording and reading are code only.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path


def _safe_id(conversation_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id or "default")[:64]
    return safe or "default"


class ConversationStore:
    def __init__(self, home):
        self.dir = Path(home) / "conversations"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, conversation_id: str) -> Path:
        return self.dir / f"{_safe_id(conversation_id)}.json"

    def record(self, conversation_id: str, role: str, text: str) -> dict:
        path = self._path(conversation_id)
        data = {"id": _safe_id(conversation_id), "turns": []}
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {"id": _safe_id(conversation_id), "turns": []}
        turn = {"role": role, "text": text, "at": datetime.now(timezone.utc).isoformat()}
        data.setdefault("turns", []).append(turn)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        return turn

    def history(self, conversation_id: str, limit: int = 50) -> list:
        path = self._path(conversation_id)
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data.get("turns", [])[-limit:]

    def context(self, conversation_id: str, limit: int = 10) -> str:
        return "\n".join(f"{t.get('role')}: {t.get('text')}" for t in self.history(conversation_id, limit=limit))

    def list_ids(self) -> list:
        return sorted(p.stem for p in self.dir.glob("*.json"))

    def forget(self, conversation_id: str) -> bool:
        path = self._path(conversation_id)
        if not path.is_file():
            return False
        path.unlink()
        return True
