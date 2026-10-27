"""Durable notification outbox, old Aidon outbox concept, rebuilt, file-based.

Notifications are queued in <home>/outbox/notifications.json, one JSON list, never lost on restart.
list() returns queued notifications, drain(sender) sends each through the caller-supplied sender,
and only removes notifications the sender reports as sent, failed sends stay queued, with their
error recorded, so a failed send is not silently dropped. No sender is built in here, the caller
supplies one, because sending depends on the transport, email, WhatsApp, Slack, or other.
"""

from __future__ import annotations

import json
from pathlib import Path


class Outbox:
    def __init__(self, home):
        self.home = Path(home)
        self.path = self.home / "outbox" / "notifications.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list) -> None:
        self.path.write_text(json.dumps(items, indent=2), encoding="utf-8")

    def queue(self, to: str, subject: str, body: str) -> dict:
        items = self._load()
        item = {"id": str(len(items) + 1), "to": to, "subject": subject, "body": body, "status": "queued"}
        items.append(item)
        self._save(items)
        return item

    def list(self) -> list:
        return self._load()

    def drain(self, sender) -> dict:
        """Send queued notifications through sender(item) -> bool, remove only sent ones, return sent and failed counts."""
        items = self._load()
        sent, failed, remaining = 0, 0, []
        for item in items:
            try:
                ok = sender(item)
            except Exception as exc:
                ok = False
                item = {**item, "status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            if ok:
                sent += 1
            else:
                failed += 1
                if item.get("status") != "failed":
                    item = {**item, "status": "failed"}
                remaining.append(item)
        self._save(remaining)
        return {"sent": sent, "failed": failed, "remaining": len(remaining)}
