"""Durable interview journal. Commit before publishing questions or accepting answers."""
from pathlib import Path
import sqlite3
import json
from contextlib import contextmanager


class InterviewStore:
    def __init__(self, home):
        self.path = Path(home) / "interviews.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL, question TEXT NOT NULL,
                lab INTEGER NOT NULL, answer TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                answered_at TEXT)""")
            db.execute("CREATE TABLE IF NOT EXISTS sessions (session_key TEXT PRIMARY KEY, document TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS lab_sessions (kind TEXT PRIMARY KEY, revision INTEGER NOT NULL, document TEXT NOT NULL)")
        self.path.chmod(0o600)

    def save_session(self, key, document):
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(session_key) DO UPDATE SET document=excluded.document",
                       (key, json.dumps(document, ensure_ascii=False)))

    def load_session(self, key):
        with self.connect() as db:
            row = db.execute("SELECT document FROM sessions WHERE session_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

    def load_lab(self, kind):
        if kind not in ("tool", "skill", "mcp"):
            raise ValueError("invalid lab")
        with self.connect() as db:
            row = db.execute("SELECT revision, document FROM lab_sessions WHERE kind=?", (kind,)).fetchone()
            return {"revision": row[0], "state": json.loads(row[1])} if row else {"revision": 0, "state": None}

    def save_lab(self, kind, state, revision):
        if kind not in ("tool", "skill", "mcp") or not isinstance(state, dict):
            raise ValueError("invalid lab state")
        turns = state.get("turns")
        if not isinstance(turns, list) or len(turns) > 500 or any(
            not isinstance(t, dict) or t.get("role") not in ("user", "assistant") or
            not isinstance(t.get("text"), str) or len(t["text"]) > 100000 for t in turns
        ):
            raise ValueError("invalid lab turns")
        document = json.dumps(state, ensure_ascii=False)
        if len(document.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("lab state too large")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT revision FROM lab_sessions WHERE kind=?", (kind,)).fetchone()
            actual = current[0] if current else 0
            if actual != revision:
                raise ValueError("lab session changed in another browser")
            db.execute("INSERT INTO lab_sessions VALUES (?,?,?) ON CONFLICT(kind) DO UPDATE SET revision=excluded.revision, document=excluded.document",
                       (kind, actual + 1, document))
            return actual + 1

    def unfinished_for_conversation(self, conversation_id, goal=None):
        """Find a single recoverable interview; refuse ambiguous selections."""
        if not conversation_id:
            return None
        matches = []
        with self.connect() as db:
            for (document,) in db.execute("SELECT document FROM sessions"):
                state = json.loads(document)
                if state.get("finished") or not state.get("goal"):
                    continue
                if goal is not None and state.get("goal") != goal:
                    continue
                owner = state.get("conversation_id")
                if owner is None:
                    run_id = (state.get("pending") or {}).get("run_id")
                    row = db.execute("SELECT conversation_id FROM turns WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
                    owner = row[0] if row else None
                if owner == conversation_id:
                    matches.append(state)
        if len(matches) > 1:
            raise ValueError("Multiple unfinished interviews; resubmit the original goal to select one")
        return matches[0] if matches else None

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def question(self, run_id, conversation_id, question, lab):
        with self.connect() as db:
            row = db.execute("INSERT INTO turns(run_id,conversation_id,question,lab) VALUES(?,?,?,?)",
                             (run_id, conversation_id, question, int(lab)))
            return row.lastrowid

    def answer(self, turn_id, answer):
        with self.connect() as db:
            row = db.execute("UPDATE turns SET answer=?, answered_at=CURRENT_TIMESTAMP WHERE id=? AND answer IS NULL",
                             (answer, turn_id))
            if row.rowcount != 1:
                raise ValueError("Question was already answered or no longer exists")

    def transcript(self, run_id):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT * FROM turns WHERE run_id=? ORDER BY id", (run_id,))]

    def last_answer(self, run_id, conversation_id, question):
        with self.connect() as db:
            row = db.execute(
                "SELECT answer FROM turns WHERE run_id=? AND conversation_id=? AND question=? ORDER BY id DESC LIMIT 1",
                (run_id, conversation_id, question)).fetchone()
            return row[0] if row else None
