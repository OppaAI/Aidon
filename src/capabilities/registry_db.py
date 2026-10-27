"""Persistent registry in SQLite, separate from the live registry view.

The registry module in this package builds a live view from the
plugin and skill stores. This module is the durable side: run
records, skill versions, and missions, in one SQLite file under
the Aidon home. Small and inspectable, no server.

The connection is wrapped with a lock because several surfaces
can touch it at once (a run thread, the WebUI, the tick loop).
Every write goes through the lock, and the database runs in WAL
mode with a busy timeout so a reader does not fail while a
writer holds the file briefly.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _LockedConnection:
    """A sqlite3 connection whose calls all take one re-entrant lock."""

    def __init__(self, path: Path):
        self._conn = sqlite3.connect(path, check_same_thread=False,
                                     timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=NORMAL")

    def execute(self, sql, params=()):
        with self._lock:
            return self._conn.execute(sql, params)

    def executescript(self, sql):
        with self._lock:
            return self._conn.executescript(sql)

    def commit(self):
        with self._lock:
            self._conn.commit()

    def rollback(self):
        with self._lock:
            self._conn.rollback()

    def close(self):
        with self._lock:
            self._conn.close()


class Registry:
    """Run records, skill versions, and missions, in <home>/registry.db."""

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "registry.db"
        self._db = _LockedConnection(self.path)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_records (
                run_id TEXT PRIMARY KEY,
                goal TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'draft',
                workspace TEXT NOT NULL DEFAULT '',
                report TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS skill_versions (
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (name, version)
            );
            CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY,
                goal TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'draft',
                run_id TEXT NOT NULL DEFAULT '',
                workspace TEXT NOT NULL DEFAULT '',
                snapshot_ref TEXT NOT NULL DEFAULT '',
                snapshot_json TEXT NOT NULL DEFAULT '',
                report TEXT NOT NULL DEFAULT '',
                history_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                channel TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_state (
                run_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (run_id, key)
            );
            """
        )
        # Migrate databases created by the earlier minimal registry schema.
        for column, definition in (
            ("run_id", "TEXT NOT NULL DEFAULT ''"),
            ("workspace", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_ref", "TEXT NOT NULL DEFAULT ''"),
            ("snapshot_json", "TEXT NOT NULL DEFAULT ''"),
            ("report", "TEXT NOT NULL DEFAULT ''"),
            ("history_json", "TEXT NOT NULL DEFAULT '[]'"),
        ):
            try:
                self._db.execute(f"ALTER TABLE missions ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass
        self._db.commit()

    # -- runs ----------------------------------------------------------

    def record_run(self, run_id: str, goal: str = "", state: str = "draft",
                   workspace: str = "", report: str = "") -> dict:
        now = _now()
        self._db.execute(
            "INSERT INTO run_records"
            " (run_id, goal, state, workspace, report, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(run_id) DO UPDATE SET goal = excluded.goal,"
            " state = excluded.state, workspace = excluded.workspace,"
            " report = excluded.report, updated_at = excluded.updated_at",
            (run_id, goal, state, workspace, report, now, now))
        self._db.commit()
        return self.get_run(run_id)

    def update_run(self, run_id: str, **fields) -> dict | None:
        allowed = {"goal", "state", "workspace", "report"}
        changes = {k: v for k, v in fields.items() if k in allowed}
        if not changes:
            return self.get_run(run_id)
        assignments = ", ".join(f"{k} = ?" for k in changes)
        self._db.execute(
            f"UPDATE run_records SET {assignments}, updated_at = ?"
            " WHERE run_id = ?",
            (*changes.values(), _now(), run_id))
        self._db.commit()
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT run_id, goal, state, workspace, report, created_at,"
            " updated_at FROM run_records WHERE run_id = ?",
            (run_id,)).fetchone()
        return dict(row) if row else None

    # -- run-state KV (crash-resume checkpoints) ---------------------

    def save_state(self, run_id: str, key: str, value: str) -> None:
        """Store a string value scoped per run; overwriting replaces it."""
        self._db.execute(
            "INSERT INTO run_state (run_id, key, value) VALUES (?, ?, ?)"
            " ON CONFLICT(run_id, key) DO UPDATE SET value = excluded.value",
            (run_id, key, str(value)))
        self._db.commit()

    def get_state(self, run_id: str, key: str) -> str:
        """Return the stored value, or "" when absent."""
        row = self._db.execute(
            "SELECT value FROM run_state WHERE run_id = ? AND key = ?",
            (run_id, key)).fetchone()
        return row["value"] if row else ""

    def clear_state(self, run_id: str) -> None:
        """Delete all state keys for one run."""
        self._db.execute("DELETE FROM run_state WHERE run_id = ?", (run_id,))
        self._db.commit()

    def list_runs(self, state: str | None = None) -> list[dict]:
        if state is None:
            rows = self._db.execute(
                "SELECT run_id, goal, state, workspace, report, created_at,"
                " updated_at FROM run_records ORDER BY created_at").fetchall()
        else:
            rows = self._db.execute(
                "SELECT run_id, goal, state, workspace, report, created_at,"
                " updated_at FROM run_records WHERE state = ?"
                " ORDER BY created_at", (state,)).fetchall()
        return [dict(row) for row in rows]

    # -- skill versions --------------------------------------------------

    def record_skill_version(self, name: str, version: int,
                             snapshot: dict | str) -> dict:
        if isinstance(snapshot, dict):
            snapshot_json = json.dumps(snapshot)
        else:
            snapshot_json = str(snapshot)
        self._db.execute(
            "INSERT INTO skill_versions (name, version, snapshot_json,"
            " created_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(name, version) DO UPDATE SET"
            " snapshot_json = excluded.snapshot_json",
            (name, int(version), snapshot_json, _now()))
        self._db.commit()
        return {"name": name, "version": int(version),
                "snapshot": json.loads(snapshot_json)}

    def list_skill_versions(self, name: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT name, version, snapshot_json, created_at"
            " FROM skill_versions WHERE name = ? ORDER BY version",
            (name,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["snapshot"] = json.loads(item.pop("snapshot_json"))
            except (ValueError, TypeError):
                item["snapshot"] = item.pop("snapshot_json")
            out.append(item)
        return out

    # -- missions (minimal) ----------------------------------------------

    def record_mission(self, mission_id: str, goal: str = "",
                       state: str = "draft") -> dict:
        now = _now()
        self._db.execute(
            "INSERT INTO missions (id, goal, state, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET goal = excluded.goal,"
            " state = excluded.state, updated_at = excluded.updated_at",
            (mission_id, goal, state, now, now))
        self._db.commit()
        return self.get_mission(mission_id)

    def save_mission(self, mission: dict) -> dict:
        now = _now()
        self._db.execute(
            "INSERT INTO missions (id, goal, state, run_id, workspace,"
            " snapshot_ref, snapshot_json, report, history_json, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET goal=excluded.goal, state=excluded.state,"
            " run_id=excluded.run_id, workspace=excluded.workspace,"
            " snapshot_ref=excluded.snapshot_ref, snapshot_json=excluded.snapshot_json,"
            " report=excluded.report, history_json=excluded.history_json,"
            " updated_at=excluded.updated_at",
            (mission.get("id", ""), mission.get("goal", ""),
             mission.get("state", "researching"), mission.get("run_id", ""),
             mission.get("workspace", ""), mission.get("snapshot_ref", ""),
             mission.get("snapshot_json", ""), mission.get("report", ""),
             mission.get("history_json", "[]"), mission.get("created_at", now),
             now))
        self._db.commit()
        return self.get_mission(mission.get("id", ""))

    def get_mission(self, mission_id: str) -> dict | None:
        row = self._db.execute(
            "SELECT id, goal, state, run_id, workspace, snapshot_ref,"
            " snapshot_json, report, history_json, created_at, updated_at FROM missions"
            " WHERE id = ?", (mission_id,)).fetchone()
        return dict(row) if row else None

    def list_missions(self, state: str | None = None, limit: int = 20) -> list[dict]:
        limit = max(1, int(limit))
        if state is None:
            rows = self._db.execute(
                "SELECT id, goal, state, run_id, workspace, snapshot_ref,"
                " snapshot_json, report, history_json, created_at, updated_at FROM missions"
                " ORDER BY created_at LIMIT ?", (limit,)).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id, goal, state, run_id, workspace, snapshot_ref,"
                " snapshot_json, report, history_json, created_at, updated_at FROM missions"
                " WHERE state = ? ORDER BY created_at LIMIT ?", (state, limit)).fetchall()
        return [dict(row) for row in rows]

    def delete_mission(self, mission_id: str) -> None:
        self._db.execute("DELETE FROM missions WHERE id = ?", (mission_id,))
        self._db.commit()

    def outbox_enqueue(self, kind: str, title: str, body: str = "",
                       channel: str = "") -> int:
        now = _now()
        cur = self._db.execute(
            "INSERT INTO notifications (kind, title, body, channel, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)", (kind, title, body, channel, now, now))
        self._db.commit()
        return int(cur.lastrowid)

    def outbox_pending(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, kind, title, body, channel, status, attempts, last_error,"
            " created_at, updated_at FROM notifications WHERE status = 'pending'"
            " ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def outbox_list(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, kind, title, body, channel, status, attempts, last_error,"
            " created_at, updated_at FROM notifications ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def outbox_mark(self, notification_id: int, status: str,
                    error: str = "") -> None:
        now = _now()
        if status == "pending":
            self._db.execute(
                "UPDATE notifications SET status=?, attempts=attempts+1,"
                " last_error=?, updated_at=? WHERE id=?",
                (status, error, now, notification_id))
        else:
            self._db.execute(
                "UPDATE notifications SET status=?, last_error=?, updated_at=? WHERE id=?",
                (status, error, now, notification_id))
        self._db.commit()

    def close(self) -> None:
        self._db.close()
