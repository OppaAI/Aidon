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
        self.path.chmod(0o600)

    def save_session(self, key, document):
        with self.connect() as db:
            db.execute("INSERT INTO sessions VALUES (?,?) ON CONFLICT(session_key) DO UPDATE SET document=excluded.document",
                       (key, json.dumps(document, ensure_ascii=False)))

    def load_session(self, key):
        with self.connect() as db:
            row = db.execute("SELECT document FROM sessions WHERE session_key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None

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
