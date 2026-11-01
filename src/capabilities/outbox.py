"""Durable notification outbox, Aidon outbox concept, rebuilt, file-based.

Notifications are queued in <home>/outbox/notifications.json, one JSON list, and survive normal
process restarts. Operations are serialized within and across processes sharing the same home.
list() returns queued notifications, drain(sender) sends each through the caller-supplied sender,
and only removes notifications the sender reports as sent, failed sends stay queued, with their
error recorded, so a failed send is not silently dropped. No sender is built in here, the caller
supplies one, because sending depends on the transport, email, WhatsApp, Slack, or other.

Cadence: each queued item carries attempts (default 0) and next_attempt_at (ISO UTC, default
now). drain() attempts only items whose next_attempt_at is due at the drain time, so a failed
send is not retried back-to-back in the same second: a failed attempt increments attempts and
pushes next_attempt_at out by 60 * attempts seconds (60s, then 120s, then 180s, ...). An item
already marked sent is skipped. Each item has a stable idempotency_key for senders to pass to
providers that support deduplication. A crash after provider acceptance but before the local queue
is saved can still cause a retry if the provider does not deduplicate it.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.RLock] = {}
_PROCESS_LOCK_STATE = threading.local()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _process_lock(path: Path) -> Iterator[None]:
    """Lock a data file across processes, reentrantly within the current thread."""
    key = str(path.resolve())
    held = getattr(_PROCESS_LOCK_STATE, "held", None)
    if held is None:
        held = _PROCESS_LOCK_STATE.held = {}
    if held.get(key, 0):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return

    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.seek(0)
                lock_file.write(b"0")
                lock_file.flush()
            while True:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)

            def unlock() -> None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            def unlock() -> None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        held[key] = 1
        try:
            yield
        finally:
            held.pop(key, None)
            unlock()


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
        with self._locked():
            if not self.path.exists():
                self._save([])

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock:
            with _process_lock(self.path):
                yield

    def _load(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except json.JSONDecodeError as exc:
            raise ValueError("notification outbox is corrupt; restore it before writing") from exc
        if (not isinstance(data, list)
                or any(not isinstance(item, dict) or not item.get("id") for item in data)):
            raise ValueError("notification outbox has invalid records; restore it before writing")
        return data

    def _save(self, items: list) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def queue(self, to: str, subject: str, body: str) -> dict:
        with self._locked():
            items = self._load()
            item_id = uuid.uuid4().hex
            item = {"id": item_id, "idempotency_key": item_id, "to": to,
                    "subject": subject, "body": body, "status": "queued",
                    "attempts": 0, "next_attempt_at": _now_iso()}
            items.append(item)
            self._save(items)
            return item

    def list(self) -> list:
        with self._locked():
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
        # Serialize drains across processes. The reentrant lock lets a sender
        # enqueue another notification on this thread; the final merge reads
        # that new item before updating the items from this drain's snapshot.
        with self._locked():
            items = self._load()
            # Give older queued records a stable key before their next send.
            changed = False
            for item in items:
                if not item.get("idempotency_key"):
                    item["idempotency_key"] = uuid.uuid4().hex
                    changed = True
            if changed:
                self._save(items)
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
            if not outcomes:
                return {"sent": sent, "failed": failed, "remaining": len(items)}
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
