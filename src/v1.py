"""v1 API and token auth: external stable API, separate from WebUI.

Design, fixed:
- External stable API is /api/v1/*, separate from
  internal WebUI /api/*, which is not built yet.
  Versioning is v1, in the path, never in a header
  alone, so a future v2 does not break v1 clients.
- Aidon generates keys. A key is shown once, at
  generation, and the caller must save it then.
  After generation, only a SHA256 hash of the key
  is stored, in a 0600 file, <home>/v1/keys.json.
  A real key is never shown on screen, in a video,
  in a log, or in a list response, only its hash
  prefix, for identification.
- Bearer token everywhere, except /api/v1/health,
  which is liveness only and returns no data.
  Missing or invalid token is 401. A valid token
  with insufficient scope is 403.
- Scopes, fixed:
  - owner: full, submit, read, steer, cancel, and
    approve, and key management.
  - agent: submit, read, steer, and cancel, only
    for goals that agent submitted, never approve,
    never key management.
- Goals, shared store for v1 API and MCP:
  A goal record has an id, a goal text, a project
  id, an owner scope name, a status (queued, running,
  done, failed, cancelled, rejected), and a steer
  note list. Submitting a goal queues it, it does
  not run it synchronously in the API handler.
  Reading, steering, and cancelling are scoped:
  an agent token sees only its own goals, an owner
  token sees all goals.

This module is testable without a socket: handle()
takes method, path, headers, and body, and returns
(status, json). The HTTP server in main.py wraps
handle(), it does not contain separate API logic.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass, field
from pathlib import Path

OWNER_SCOPES = ("submit", "read", "steer", "cancel", "approve", "keys")
AGENT_SCOPES = ("submit", "read", "steer", "cancel")


@dataclass
class KeyRecord:
    name: str
    scope: str
    key_hash: str
    created_at: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "scope": self.scope,
                "key_hash": self.key_hash, "created_at": self.created_at}


class AuthStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.keys_dir = self.home / "v1"
        self.keys_dir.mkdir(parents=True, exist_ok=True)
        self.keys_path = self.keys_dir / "keys.json"
        self.keys: list[KeyRecord] = []
        self._load()

    def _load(self) -> None:
        if not self.keys_path.exists():
            return
        try:
            data = json.loads(self.keys_path.read_text(encoding="utf-8"))
            self.keys = [KeyRecord(**record) for record in data.get("keys", [])]
        except (json.JSONDecodeError, TypeError):
            self.keys = []

    def _save(self) -> None:
        self.keys_path.write_text(
            json.dumps({"keys": [k.to_dict() for k in self.keys]}, indent=2),
            encoding="utf-8")
        try:
            self.keys_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def create_key(self, name: str, scope: str) -> tuple[str, KeyRecord]:
        """Generate a key, return it once, store only its hash."""
        if scope not in ("owner", "agent"):
            raise ValueError("scope must be owner or agent")
        key = "aidon_" + secrets.token_urlsafe(32)
        record = KeyRecord(name=name, scope=scope, key_hash=self._hash(key))
        self.keys = [k for k in self.keys if k.name != name]
        self.keys.append(record)
        self._save()
        return key, record

    def verify(self, token: str | None) -> KeyRecord | None:
        if not token:
            return None
        if token.startswith("Bearer "):
            token = token[7:]
        digest = self._hash(token)
        for record in self.keys:
            if record.key_hash == digest:
                return record
        return None

    def scopes_for(self, record: KeyRecord) -> tuple[str, ...]:
        return OWNER_SCOPES if record.scope == "owner" else AGENT_SCOPES

    def revoke(self, name: str) -> bool:
        """Revoke a key by name, immediate, hash removed, never the key itself."""
        before = len(self.keys)
        self.keys = [k for k in self.keys if k.name != name]
        if len(self.keys) == before:
            return False
        self._save()
        return True

    def list_keys(self) -> list[dict]:
        # Hash prefix only, never a real key.
        return [{"name": k.name, "scope": k.scope,
                 "key_hash_prefix": k.key_hash[:12]} for k in self.keys]


@dataclass
class GoalRecord:
    id: str
    goal: str
    project_id: str = "default"
    owner_name: str = ""
    status: str = "queued"
    steer_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "goal": self.goal,
                "project_id": self.project_id, "owner_name": self.owner_name,
                "status": self.status, "steer_notes": self.steer_notes}


class GoalStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.store_dir = self.home / "v1"
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.store_path = self.store_dir / "goals.json"
        self.goals: dict[str, GoalRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
            for record in data.get("goals", []):
                goal = GoalRecord(**record)
                self.goals[goal.id] = goal
        except (json.JSONDecodeError, TypeError):
            self.goals = {}

    def _save(self) -> None:
        self.store_path.write_text(
            json.dumps({"goals": [g.to_dict() for g in self.goals.values()]}, indent=2),
            encoding="utf-8")

    def submit(self, goal: str, project_id: str, owner_name: str) -> GoalRecord:
        record = GoalRecord(id=uuid.uuid4().hex[:12], goal=goal,
                            project_id=project_id, owner_name=owner_name)
        self.goals[record.id] = record
        self._save()
        return record

    def get(self, goal_id: str) -> GoalRecord | None:
        return self.goals.get(goal_id)

    def visible_to(self, record: KeyRecord) -> list[GoalRecord]:
        if record.scope == "owner":
            return list(self.goals.values())
        return [g for g in self.goals.values() if g.owner_name == record.name]


def handle(auth: AuthStore, goals: GoalStore, method: str, path: str,
           headers: dict, body: dict | None) -> tuple[int, dict]:
    """v1 API handler, no socket, testable directly."""
    body = body or {}
    if path == "/api/v1/health" and method == "GET":
        return 200, {"status": "ok", "version": "v1"}
    record = auth.verify(headers.get("Authorization", ""))
    if record is None:
        return 401, {"error": "missing or invalid token"}
    scopes = auth.scopes_for(record)

    def need(scope: str) -> tuple[int, dict] | None:
        if scope not in scopes:
            return 403, {"error": "insufficient scope"}
        return None

    if path == "/api/v1/keys" and method == "POST":
        denied = need("keys")
        if denied:
            return denied
        try:
            key, key_record = auth.create_key(str(body.get("name", "")),
                                              str(body.get("scope", "agent")))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        return 201, {"key": key, "name": key_record.name, "scope": key_record.scope}
    if path == "/api/v1/keys" and method == "GET":
        denied = need("keys")
        if denied:
            return denied
        return 200, {"keys": auth.list_keys()}
    if path == "/api/v1/keys" and method == "DELETE":
        denied = need("keys")
        if denied:
            return denied
        ok = auth.revoke(str(body.get("name", "")))
        return (200, {"ok": True}) if ok else (404, {"error": "key not found"})
    if path == "/api/v1/goals" and method == "POST":
        denied = need("submit")
        if denied:
            return denied
        goal = goals.submit(str(body.get("goal", "")),
                            str(body.get("project_id", "default")),
                            owner_name=record.name)
        return 201, goal.to_dict()
    if path == "/api/v1/goals" and method == "GET":
        denied = need("read")
        if denied:
            return denied
        return 200, {"goals": [g.to_dict() for g in goals.visible_to(record)]}
    if path.startswith("/api/v1/goals/"):
        goal_id = path.split("/")[4] if len(path.split("/")) > 4 else ""
        # Path forms: /api/v1/goals/<id>, /api/v1/goals/<id>/cancel,
        # /api/v1/goals/<id>/steer, /api/v1/goals/<id>/approve
        parts = path.split("/")
        goal_id = parts[4] if len(parts) > 4 else ""
        action = parts[5] if len(parts) > 5 else ""
        goal = goals.get(goal_id)
        if goal is None:
            return 404, {"error": "goal not found"}
        if record.scope == "agent" and goal.owner_name != record.name:
            return 403, {"error": "not your goal"}
        if method == "GET" and not action:
            denied = need("read")
            return denied if denied else (200, goal.to_dict())
        if method == "POST" and action == "cancel":
            denied = need("cancel")
            if denied:
                return denied
            goal.status = "cancelled"
            goals._save()
            try:
                from .run_control import RunControl
                RunControl(goals.home).cancel(goal_id)
            except Exception:
                pass
            return 200, goal.to_dict()
        if method == "POST" and action == "steer":
            denied = need("steer")
            if denied:
                return denied
            goal.steer_notes.append(str(body.get("note", "")))
            goals._save()
            try:
                from .run_control import RunControl
                control = RunControl(goals.home)
                if control.get(goal_id) is None:
                    control.register(goal_id, goal.goal, goal.project_id)
                control.steer(goal_id, str(body.get("note", "")))
            except Exception:
                pass
            return 200, goal.to_dict()
        if method == "POST" and action == "approve":
            denied = need("approve")
            if denied:
                return denied
            goal.status = "approved"
            goals._save()
            return 200, goal.to_dict()
        return 404, {"error": "unknown v1 route"}
    return 404, {"error": "unknown v1 route"}
