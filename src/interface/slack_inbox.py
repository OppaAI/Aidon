"""Durable Slack goals, processed separately from command/history polling.

Polling must remain available while a goal waits for approval or a question.
History is checkpointed only after a goal is durably queued. Pending jobs
survive restart; interrupted runs require owner review before another attempt
because their earlier steps may already have changed external state.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from ..capabilities.outbox import _process_lock
from .slack_polling import dispatch_slack_message
from .triggers import TriggerEvent


class SlackInbox:
    def __init__(self, home, dispatcher, sender, ack_after: float = 15, recorder=None):
        self.path = Path(home) / "messaging" / "slack" / "inbox.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.dispatcher = dispatcher
        self.sender = sender
        self.ack_after = ack_after
        self.recorder = recorder
        self._stop = threading.Event()
        self._worker = None
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS messages ("
                       "channel TEXT NOT NULL, ts TEXT NOT NULL, event TEXT NOT NULL, "
                       "status TEXT NOT NULL DEFAULT 'pending', error TEXT NOT NULL DEFAULT '', "
                       "PRIMARY KEY(channel, ts))")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, event: TriggerEvent) -> None:
        channel = str(event.payload.get("channel", ""))
        timestamp = str(event.payload.get("slack_ts", ""))
        if not channel or not timestamp:
            raise ValueError("Slack message needs a channel and timestamp")
        data = json.dumps({"source": event.source, "kind": event.kind,
                           "goal": event.goal, "conversation_id": event.conversation_id,
                           "payload": event.payload})
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO messages (channel, ts, event) VALUES (?, ?, ?)",
                       (channel, timestamp, data))

    def recover(self):
        """Call only under the lifetime worker lock, after a prior process exited."""
        with self._connect() as db:
            interrupted = db.execute(
                "SELECT channel, ts FROM messages WHERE status='running'").fetchall()
            db.execute("UPDATE messages SET status='interrupted', error='process restarted' "
                       "WHERE status='running'")
        for row in interrupted:
            self.sender(row["channel"],
                        "Aidon restarted while handling this goal. Review the run in "
                        "the WebUI before sending it again; earlier steps may have completed.",
                        row["ts"])

    def process_once(self) -> bool:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT rowid, * FROM messages WHERE status='pending' "
                             "ORDER BY rowid LIMIT 1").fetchone()
            if row is None:
                return False
            db.execute("UPDATE messages SET status='running' WHERE rowid=?", (row["rowid"],))
        try:
            event = TriggerEvent(**json.loads(row["event"]))
            if self.recorder is not None:
                self.recorder(event.conversation_id, "user", event.goal)

            def reply(channel, text, thread_ts=None):
                result = self.sender(channel, text, thread_ts)
                if self.recorder is not None:
                    self.recorder(event.conversation_id, "assistant", text)
                return result

            dispatch_slack_message(
                self.dispatcher, event, channel=row["channel"],
                thread_ts=event.payload.get("thread_ts") or row["ts"],
                sender=reply, ack_after=self.ack_after)
        except Exception as exc:
            # Do not re-execute a completed goal merely because reporting
            # failed; retain the failed record for owner diagnosis.
            state, error = "failed", type(exc).__name__
            print(f"Slack inbox failed for {row['channel']}: {error}")
        else:
            state, error = "done", ""
        with self._connect() as db:
            db.execute("UPDATE messages SET status=?, error=? WHERE rowid=?",
                       (state, error, row["rowid"]))
            db.execute("DELETE FROM messages WHERE status='done' AND rowid NOT IN "
                       "(SELECT rowid FROM messages WHERE status='done' ORDER BY rowid DESC LIMIT 1000)")
        return True

    def start(self):
        if self._worker is not None:
            return

        def work():
            # One consumer across processes; producers can still enqueue.
            with _process_lock(self.path.with_name("worker")):
                if self._stop.is_set():
                    return
                self.recover()
                while not self._stop.is_set():
                    try:
                        if self.process_once():
                            continue
                    except Exception as exc:
                        print(f"Slack inbox worker error: {type(exc).__name__}")
                    self._stop.wait(1)

        self._worker = threading.Thread(target=work, daemon=True, name="slack-goals")
        self._worker.start()

    def close(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1)
