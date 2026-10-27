"""Durable notification outbox, old Aidon outbox concept, rebuilt, file-based.

Notifications are queued in <home>/outbox/notifications.json, one JSON list, never lost on restart.
list() returns queued notifications, drain(sender) sends each through the caller-supplied sender,
and only removes notifications the sender reports as sent, failed sends stay queued, with their
error recorded, so a failed send is not silently dropped. No sender is built in here, the caller
supplies one, because sending depends on the transport, email, WhatsApp, Slack, or other.

Cadence: each queued item carries attempts (default 0) and next_attempt_at (ISO UTC, default
now). drain() attempts only items whose next_attempt_at is due at the drain time, so a failed
send is not retried back-to-back in the same second: a failed attempt increments attempts and
pushes next_attempt_at out by 60 * attempts seconds (60s, then 120s, then 180s, ...). An item
already marked sent is never sent twice — drain skips it, and a sent item is removed exactly
once, by the drain that sent it.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _now_iso(now: datetime | None = None) -> str:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat()


def _parse_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_now(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


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
        item = {"id": str(len(items) + 1), "to": to, "subject": subject, "body": body, "status": "queued",
                "attempts": 0, "next_attempt_at": _now_iso()}
        items.append(item)
        self._save(items)
        return item

    def list(self) -> list:
        return self._load()

    def drain(self, sender, now: datetime | None = None) -> dict:
        """Send due queued notifications through sender(item) -> bool.

        Only items whose next_attempt_at is at or before now are
        attempted (now defaults to the current UTC time). A sent item
        is removed exactly once; an item already marked sent is
        skipped, never sent twice. A failed attempt (a False return
        or an exception) increments attempts and reschedules the
        item to now + 60 * attempts seconds. Returns sent, failed,
        and remaining counts, same shape as before.
        """
        moment = _as_now(now)
        items = self._load()
        sent, failed, remaining = 0, 0, []
        for item in items:
            if item.get("status") == "sent":
                # Already delivered: never sent twice, and no longer
                # pending, so it leaves the queue without counting
                # as sent by this drain.
                continue
            due_at = _parse_iso(item.get("next_attempt_at"))
            if due_at is not None and due_at > moment:
                remaining.append(item)
                continue
            try:
                ok = sender(item)
            except Exception as exc:
                ok = False
                item = {**item, "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}"}
            if ok:
                sent += 1
            else:
                failed += 1
                attempts = int(item.get("attempts") or 0) + 1
                item = {**item, "status": "failed", "attempts": attempts,
                        "next_attempt_at": (moment + timedelta(seconds=60 * attempts)).isoformat()}
                remaining.append(item)
        self._save(remaining)
        return {"sent": sent, "failed": failed, "remaining": len(remaining)}
