"""Durable conversations in SQLite, separate from the JSON store.

The JSON store in conversations.py is a plain transcript file. This
module is the durable side: conversations, numbered
exchanges, attachments, ideas, and a rolling summary per
conversation, all in one SQLite file under the Aidon home.

The projects table and project_id columns below are legacy storage
from the removed projects concept. They are kept so existing homes
keep opening without a migration; no code reads or writes them
for scoping anymore (new rows store an empty string).

Exchange numbering is the part that needs care. Two writers (the
WebUI and a messaging worker, for example) must never be handed
the same exchange number, so numbering happens inside one
serialised transaction: begin immediately, read the current
maximum, insert the next number, commit. A second writer waits
on the write lock instead of computing from a stale maximum.

The rolling summary is code, not a model call: the last few
exchanges are condensed to one paragraph by taking the first
sentence of each, so a long conversation stays readable without
another round trip.
"""

from __future__ import annotations

import sqlite3
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

_SUMMARY_EXCHANGES = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


def _first_sentence(text: str) -> str:
    text = (text or "").strip().replace("\n", " ")
    for mark in (". ", "! ", "? "):
        if mark in text:
            return text.split(mark)[0].strip() + mark.strip()
    return text[:200].strip()


class ConversationStoreDB:
    """SQLite-backed conversation store under <home>/conversations.db."""

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "conversations.db"
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exchanges (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    exchange_no INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    text TEXT NOT NULL,
                    route_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE (conversation_id, exchange_no)
                );
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ideas (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    conversation_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            # Existing conversation databases predate persisted routing
            # metadata. Keep their transcripts intact and add the nullable
            # detail needed to restore route badges after a reload.
            exchange_columns = {
                row["name"] for row in self._conn.execute(
                    "PRAGMA table_info(exchanges)").fetchall()
            }
            if "route_json" not in exchange_columns:
                self._conn.execute(
                    "ALTER TABLE exchanges ADD COLUMN route_json TEXT NOT NULL DEFAULT ''")
            self._conn.commit()

    # -- conversations -------------------------------------------------

    def create_conversation(self, conversation_id: str | None = None,
                            return_created: bool = False) -> dict:
        conversation_id = str(conversation_id or _new_id())
        if (len(conversation_id) > 128 or not conversation_id
                or any(ch not in "0123456789abcdef-" for ch in conversation_id.lower())):
            raise ValueError("invalid conversation ID")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO conversations (id, project_id, created_at)"
                " VALUES (?, ?, ?)", (conversation_id, "", _now()))
            self._conn.commit()
        record = {"id": conversation_id}
        return (record, cursor.rowcount == 1) if return_created else record

    def list_all_conversations(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, created_at FROM conversations"
                " ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def import_conversation(self, conversation_id: str,
                            turns: list | None = None) -> dict:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO conversations (id, project_id, created_at)"
                " VALUES (?, ?, ?)", (conversation_id, "", _now()))
            self._conn.commit()
        for turn in turns or []:
            if isinstance(turn, dict):
                self.record_exchange(conversation_id,
                                     str(turn.get("role", "user")),
                                     str(turn.get("text", "")))
            else:
                self.record_exchange(conversation_id, "user", str(turn))
        return {"id": conversation_id}

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = self._conn.execute(
                    "DELETE FROM conversations WHERE id = ?",
                    (conversation_id,))
                self._conn.execute(
                    "DELETE FROM exchanges WHERE conversation_id = ?",
                    (conversation_id,))
                self._conn.execute(
                    "DELETE FROM attachments WHERE conversation_id = ?",
                    (conversation_id,))
                self._conn.execute(
                    "DELETE FROM summaries WHERE conversation_id = ?",
                    (conversation_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return cur.rowcount > 0

    # -- exchanges -----------------------------------------------------

    def record_exchange(self, conversation_id: str, role: str,
                        text: str, route: dict | None = None) -> dict:
        """Append one exchange, numbered under a serialised transaction."""
        route_data = ""
        if isinstance(route, dict) and route.get("group"):
            try:
                route_data = json.dumps({
                    "group": str(route["group"]),
                    "confidence": max(0.0, min(1.0, float(route["confidence"]))),
                }, separators=(",", ":"))
            except (KeyError, TypeError, ValueError):
                route_data = ""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(exchange_no), 0) AS m"
                    " FROM exchanges WHERE conversation_id = ?",
                    (conversation_id,)).fetchone()
                number = int(row["m"]) + 1
                self._conn.execute(
                    "INSERT INTO exchanges"
                    " (conversation_id, exchange_no, role, text, route_json, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (conversation_id, number, role, text, route_data, _now()))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        self._refresh_summary(conversation_id)
        return {"conversation_id": conversation_id, "exchange_no": number,
                "role": role, "text": text,
                "route": json.loads(route_data) if route_data else None}

    def history(self, conversation_id: str, limit: int = 50) -> list[dict]:
        # Reads take the same lock as writes: one sqlite3 connection is
        # shared by every thread, and a read that runs while another
        # thread is inside BEGIN IMMEDIATE raises InterfaceError
        # ("bad parameter or other API misuse") on the connection.
        with self._lock:
            rows = self._conn.execute(
                "SELECT exchange_no, role, text, route_json, created_at FROM exchanges"
                " WHERE conversation_id = ? ORDER BY exchange_no DESC LIMIT ?",
                (conversation_id, limit)).fetchall()
        history = []
        for row in reversed(rows):
            item = dict(row)
            route_data = item.pop("route_json", "")
            try:
                route = json.loads(route_data) if route_data else None
            except (TypeError, ValueError):
                route = None
            if isinstance(route, dict):
                item["route"] = route
            history.append(item)
        return history

    # -- summary -------------------------------------------------------

    def _refresh_summary(self, conversation_id: str) -> None:
        # One critical section for the read and the write, so the
        # summary always matches the exchange that triggered it and
        # never races another writer on the shared connection.
        with self._lock:
            recent = self.history(conversation_id, limit=_SUMMARY_EXCHANGES)
            if not recent:
                return
            parts = [f"{row['role']}: {_first_sentence(row['text'])}"
                     for row in recent]
            summary = " ".join(parts)
            self._conn.execute(
                "INSERT INTO summaries (conversation_id, text, updated_at)"
                " VALUES (?, ?, ?) ON CONFLICT(conversation_id) DO UPDATE"
                " SET text = excluded.text, updated_at = excluded.updated_at",
                (conversation_id, summary, _now()))
            self._conn.commit()

    def rolling_summary(self, conversation_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT text FROM summaries WHERE conversation_id = ?",
                (conversation_id,)).fetchone()
        return row["text"] if row else ""

    # -- attachments and ideas -----------------------------------------

    def add_attachment(self, conversation_id: str, path: str,
                       kind: str = "") -> dict:
        attachment_id = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO attachments (id, conversation_id, path, kind,"
                " created_at) VALUES (?, ?, ?, ?, ?)",
                (attachment_id, conversation_id, path, kind, _now()))
            self._conn.commit()
        return {"id": attachment_id, "conversation_id": conversation_id,
                "path": path, "kind": kind}

    def add_idea(self, text: str) -> dict:
        idea_id = _new_id()
        with self._lock:
            self._conn.execute(
                "INSERT INTO ideas (id, project_id, text, created_at)"
                " VALUES (?, ?, ?, ?)", (idea_id, "", text, _now()))
            self._conn.commit()
        return {"id": idea_id, "text": text}


    def close(self) -> None:
        with self._lock:
            self._conn.close()


def record_run_exchange(store: ConversationStoreDB, conversation_id: str,
                        role: str, text: str) -> dict:
    """Record one run exchange on a store, the module-level entry point."""
    return store.record_exchange(conversation_id, role, text)
