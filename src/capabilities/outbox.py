"""Durable notification outbox, Aidon outbox concept, rebuilt, file-based.

Notifications are queued in <home>/outbox/notifications.json, one JSON list, and survive normal
process restarts. Operations are serialized within one process, while independent processes sharing
the same home are not coordinated.
list() returns queued notifications, drain(sender) sends each through the caller-supplied sender,
and only removes notifications the sender reports as sent, failed sends stay queued, with their
error recorded, so a failed send is not silently dropped. No sender is built in here, the caller
supplies one, because sending depends on the transport, email, WhatsApp, Slack, or other.

Cadence: each queued item carries attempts (default 0) and next_attempt_at (ISO UTC, default
now). drain() attempts only items whose next_attempt_at is due at the drain time, so a failed
send is not retried back-to-back in the same second: a failed attempt increments attempts and
pushes next_attempt_at out by 60 * attempts seconds (60s, then 120s, then 180s, ...). An item
already marked sent is skipped. A process crash after the external sender accepts a message but
before the local queue is saved can cause a retry, so delivery is at-least-once across that window.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


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
        self._lock = _lock_for(self.path)
        with self._lock:
            if not self.path.exists():
                self._save([])

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(items, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def queue(self, to: str, subject: str, body: str) -> dict:
        with self._lock:
            items = self._load()
            numeric_ids = [int(x["id"]) for x in items
                           if isinstance(x, dict)
                           and str(x.get("id", "")).isdigit()]
            item = {"id": str(max(numeric_ids, default=0) + 1), "to": to,
                    "subject": subject, "body": body, "status": "queued",
                    "attempts": 0, "next_attempt_at": _now_iso()}
            items.append(item)
            self._save(items)
            return item

    def list(self) -> list:
        with self._lock:
            return self._load()

    def drain(self, sender, now: datetime | None = None) -> dict:
        """Send due queued notifications through sender(item) -> bool.

        Only items whose next_attempt_at is at or before now are
        attempted (now defaults to the current UTC time). An item
        already marked sent is skipped. A failed attempt (a False return
        or an exception) increments attempts and reschedules the
        item to now + 60 * attempts seconds. Returns sent, failed,
        and remaining counts, same shape as before.
        """
        moment = _as_now(now)
        # Serialize drains for this path. The reentrant lock lets a sender
        # enqueue another notification on this thread; the final merge reads
        # that new item before updating the items from this drain's snapshot.
        with self._lock:
            items = self._load()
            outcomes: dict[str, dict | None] = {}
            sent = failed = 0
            for item in items:
                if item.get("status") == "sent":
                    outcomes[str(item.get("id", ""))] = None
                    continue
                due_at = _parse_iso(item.get("next_attempt_at"))
                if due_at is not None and due_at > moment:
                    continue
                try:
                    ok = sender(item)
                except Exception as exc:
                    ok = False
                    item = {**item, "error": f"{type(exc).__name__}: {exc}"}
                item_id = str(item.get("id", ""))
                if ok:
                    sent += 1
                    outcomes[item_id] = None
                else:
                    failed += 1
                    attempts = int(item.get("attempts") or 0) + 1
                    outcomes[item_id] = {
                        **item, "status": "failed", "attempts": attempts,
                        "next_attempt_at": (moment + timedelta(
                            seconds=60 * attempts)).isoformat(),
                    }
            # Merge against the latest file so enqueues performed by the
            # sender are retained. Items removed by another API remain gone.
            latest = self._load()
            remaining = []
            for item in latest:
                item_id = str(item.get("id", ""))
                if item_id in outcomes:
                    updated = outcomes[item_id]
                    if updated is not None:
                        remaining.append(updated)
                else:
                    remaining.append(item)
            self._save(remaining)
            return {"sent": sent, "failed": failed,
                    "remaining": len(remaining)}
