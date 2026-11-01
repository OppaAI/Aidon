"""Persist chat replies before transport so a failed send can be retried.

This queue is separate from owner notifications. Retrying a reply never
re-runs the goal that produced it. Providers without idempotency support
can still duplicate a message if the process dies after remote acceptance.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..capabilities.outbox import Outbox


class ReplySender:
    def __init__(self, home, surface: str, transport):
        if surface not in ("slack", "whatsapp"):
            raise ValueError("unsupported reply surface")
        self.surface = surface
        self.transport = transport
        self.outbox = Outbox(Path(home) / "messaging" / surface)
        self._stop = threading.Event()
        self._worker = None

    def __call__(self, recipient: str, text: str, thread_ts=None) -> dict:
        item = self.outbox.queue(str(recipient), str(thread_ts or ""), str(text))
        self.retry_due()
        queued = any(record["id"] == item["id"] for record in self.outbox.list())
        return {"ok": not queued, "queued": queued,
                **({"error": f"{self.surface} reply saved for retry"} if queued else {})}

    def retry_due(self, now=None) -> dict:
        def attempt(item):
            result = self.transport(item["to"], item["body"], item["subject"] or None)
            ok = isinstance(result, dict) and bool(result.get("ok"))
            if not ok:
                # The transport logs its safe status; never log message bodies.
                print(f"{self.surface.capitalize()} reply queued for retry")
            return ok

        return self.outbox.drain(attempt, now=now)

    def start(self, interval_seconds: float = 10) -> None:
        if self._worker is not None:
            return

        def retry_loop():
            while not self._stop.is_set():
                try:
                    self.retry_due()
                except Exception as exc:
                    print(f"{self.surface.capitalize()} reply retry failed: {type(exc).__name__}")
                self._stop.wait(max(1, interval_seconds))

        self._worker = threading.Thread(
            target=retry_loop, daemon=True, name=f"{self.surface}-replies")
        self._worker.start()

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1)
