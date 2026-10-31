"""Conversation, artifact, and lightweight WebUI data surfaces."""

import json
import threading
from pathlib import Path


class WebUIDataMixin:
    @staticmethod
    def _safe_name(name: str) -> str:
        safe = "".join(c for c in str(name) if c.isalnum() or c in ("-", "_", "."))
        return safe or "item"

    def _json_dir(self, subdir: str) -> Path:
        path = self.home / subdir
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _read_json_file(self, path: Path):
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _write_json_file(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def handle_tools(self) -> dict:
        from ..capabilities.plugins import PluginRegistry
        from ..capabilities.tool_catalog import all_tool_names
        from ..capabilities.tool_discovery import list_tools as discover_tools
        registry = PluginRegistry(self.home)
        registry.load_all()
        plugin_tools = registry.tool_names()
        # Keep the static core manifest for built-in tools, but merge the
        # live registry as well. Adopted tools therefore cannot disappear
        # from the WebUI just because the manifest was not edited.
        live_names = [str(item.get("name", "")) for item in discover_tools(self.home)
                      if item.get("name")]
        selfmod = (str(getattr(self.config, "tool_posture", "standard")
                       or "standard").strip().lower() == "selfmod"
                   and not getattr(self.config, "kernel_mode", False))
        kernel_mode = bool(getattr(self.config, "kernel_mode", False))
        optional_tools = bool(getattr(self.config, "optional_tools", False))
        goal_tools = list(dict.fromkeys(
            all_tool_names(plugin_tools, selfmod=selfmod) + live_names))
        sandbox_tools = ["write", "read", "list", "shell", "calculate"]
        if kernel_mode:
            from ..agentic.kernel import SEED_TOOLS
            sandbox_tools = [name for name in sandbox_tools if name in SEED_TOOLS]
            goal_tools = [name for name in goal_tools if name in SEED_TOOLS]
            # Kernel mode blocks plugin dispatch even if an adopted plugin
            # remains installed in the registry.
            plugin_tools = []
        elif not optional_tools:
            from ..agentic.kernel import CORE_LADDER_TOOLS
            always_available = (CORE_LADDER_TOOLS
                                | {"write", "read", "list", "shell", "calculate",
                                   "read_file", "write_file",
                                   "capture_webcam_photo", "record_webcam",
                                   "record_system_audio"}
                                | set(plugin_tools))
            goal_tools = [name for name in goal_tools if name in always_available]
        return {"sandbox_tools": sandbox_tools,
                "goal_tools": goal_tools,
                "plugin_tools": plugin_tools}

    _FILE_CONTENT_TYPES = {
        ".md": "text/markdown", ".txt": "text/plain",
        ".html": "text/html", ".json": "application/json",
        ".pdf": "application/pdf", ".png": "image/png",
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }

    def handle_file(self, path: str) -> tuple[int, str, bytes]:
        """Serve one file from under the server's home directory.

        Returns (status, content_type, body). The requested path is
        resolved (symlinks followed) BEFORE the confinement check,
        so a symlink inside home pointing outside home is refused
        with 403, exactly like a literal traversal. A missing file
        is 404, a directory is 400. Nothing outside home is ever
        served, so this endpoint cannot become a way to read /etc.
        """
        home = Path(self.home).resolve()
        candidate = Path(path) if Path(path).is_absolute() else home / path
        try:
            resolved = candidate.resolve()
        except OSError:
            return 404, "application/json", b'{"error": "not found"}'
        if not resolved.is_relative_to(home):
            return 403, "application/json", b'{"error": "outside home"}'
        relative = resolved.relative_to(home)
        allowed_roots = (home / "workspace", home / "library" / "files", home / "images")
        if (any(part.startswith(".") for part in relative.parts)
                or resolved.suffix.lower() in (".db", ".sqlite", ".sqlite3", ".pem", ".key")
                or not any(resolved.is_relative_to(root.resolve()) for root in allowed_roots)):
            return 403, "application/json", b'{"error": "not a public artifact"}'
        if resolved.is_dir():
            return 400, "application/json", b'{"error": "is a directory"}'
        if not resolved.is_file():
            return 404, "application/json", b'{"error": "not found"}'
        content_type = self._FILE_CONTENT_TYPES.get(
            resolved.suffix.lower(), "application/octet-stream")
        return 200, content_type, resolved.read_bytes()

    def _conversations_db(self):
        from ..memory.conversations_db import ConversationStoreDB
        return ConversationStoreDB(self.home)

    def handle_conversations_list(self) -> dict:
        # Single write path is the SQLite store; legacy JSON
        # records are still listed (read-only) until imported.
        try:
            db = self._conversations_db()
            records = db.list_all_conversations()
            seen = {r["id"] for r in records}
        except Exception:
            records, seen = [], set()
        directory = self._json_dir("conversations")
        for path in sorted(directory.glob("*.json")):
            data = self._read_json_file(path)
            if data is not None and data.get("id") not in seen:
                records.append(data)
        # Session names come from the first user interaction. Keep this
        # derived rather than asking the model for a title, so naming is
        # immediate, deterministic, and survives restarts.
        for record in records:
            if record.get("title") and record.get("exchange_count"):
                continue
            try:
                turns = self.handle_conversation_get(str(record.get("id", ""))) or {}
                turns = turns.get("turns", [])
                first_user = next((str(turn.get("text", "")).strip()
                                   for turn in turns
                                   if isinstance(turn, dict) and turn.get("role") == "user"), "")
                if first_user:
                    title = " ".join(first_user.split())
                    record["title"] = title[:72] + ("…" if len(title) > 72 else "")
                    record["exchange_count"] = len(turns)
                    record["updated_at"] = turns[-1].get("created_at", "") if turns else ""
            except Exception:
                pass
        return {"conversations": records}

    def handle_conversation_create(self, body: dict) -> dict:
        try:
            db = self._conversations_db()
            requested_id = str(body.get("id", "") or "")
            record, created = db.create_conversation(
                requested_id or None, return_created=True)
            if created:
                for turn in body.get("turns", []):
                    if isinstance(turn, dict):
                        db.record_exchange(record["id"], str(turn.get("role", "user")),
                                           str(turn.get("text", "")))
                    else:
                        db.record_exchange(record["id"], "user", str(turn))
            record["turns"] = db.history(record["id"])
            return record
        except Exception:
            import uuid
            conversation_id = str(body.get("id", "")) or uuid.uuid4().hex[:12]
            record = {"id": conversation_id,
                      "turns": list(body.get("turns", []))}
            self._write_json_file(
                self._json_dir("conversations") / f"{self._safe_name(conversation_id)}.json",
                record)
            return record

    def handle_conversation_get(self, conversation_id: str):
        try:
            db = self._conversations_db()
            history = db.history(conversation_id)
            ids = {r["id"] for r in db.list_all_conversations()}
            if conversation_id in ids:
                with self._question_lock:
                    waiter = self._question_by_conversation.get(conversation_id)
                    record = self._web_runs.get(waiter["run_id"]) if waiter else None
                    active_run = ({"run_id": waiter["run_id"],
                                   "event_total": len(record.get("events", [])),
                                   "trace_total": len(record.get("trace", [])),
                                   "question": waiter["question"], "lab": waiter.get("lab", False)}
                                  if waiter and record else None)
                from .interview_store import InterviewStore
                recovery = None
                try:
                    journal = InterviewStore(self.home)
                    lab_turns = journal.lab_transcript(conversation_id)
                    saved = journal.unfinished_for_conversation(conversation_id)
                    if saved:
                        pending = saved.get("pending") or {}
                        recovery = {
                            "goal": str(saved.get("goal", "")),
                            "stage": str(pending.get("kind") or saved.get("outcome") or "interview"),
                        }
                except (OSError, ValueError):
                    lab_turns = []
                    recovery = None
                return {"id": conversation_id, "turns": history, "lab_turns": lab_turns, "active_run": active_run, "recovery": recovery}
        except Exception:
            pass
        return self._read_json_file(
            self.home / "conversations" / f"{self._safe_name(conversation_id)}.json")

    def handle_conversation_delete(self, conversation_id: str) -> dict:
        try:
            db = self._conversations_db()
            if db.delete_conversation(conversation_id):
                try:
                    brief = self.home / "chat_briefs" / f"{self._safe_name(conversation_id)}.json"
                    if brief.is_file():
                        brief.unlink()
                except Exception:
                    pass
                return {"ok": True}
        except Exception:
            pass
        path = self.home / "conversations" / f"{self._safe_name(conversation_id)}.json"
        if not path.is_file():
            return {"ok": False}
        path.unlink()
        try:
            brief = self.home / "chat_briefs" / f"{self._safe_name(conversation_id)}.json"
            if brief.is_file():
                brief.unlink()
        except Exception:
            pass
        return {"ok": True}

    def handle_practice_list(self) -> dict:
        """All practice schedules, with their session history."""
        from .practice import load_schedules
        return {"schedules": load_schedules(self.home)}

    def handle_practice_create(self, body: dict) -> dict:
        """Create a practice schedule from the Practice page or CLI shape."""
        from .practice import add_schedule
        schedule, error = add_schedule(self.home, body or {})
        if error is not None:
            return {"error": error}
        return {"schedule": schedule}

    def handle_practice_delete(self, body: dict) -> dict:
        """Delete a practice schedule by name."""
        from .practice import remove_schedule
        return {"deleted": remove_schedule(self.home, str((body or {}).get("name", "")))}

    def handle_practice_run(self, body: dict) -> dict:
        """Start a practice session now, in the background.

        Run-now uses a 60-minute window from now, not the
        schedule's window, and does not consume the day's
        scheduled session. It needs the serve daemon's
        practice_session_runner; without the daemon it
        reports that honestly instead of silently doing
        nothing.
        """
        from datetime import datetime, timedelta
        from .practice import get_schedule
        if self.practice_session_runner is None:
            return {"error": "practice runs need the serve daemon (launch.sh serve)"}
        schedule = get_schedule(self.home, str((body or {}).get("name", "")))
        if schedule is None:
            return {"error": "no such practice schedule"}
        end = datetime.now() + timedelta(minutes=60)
        runner = self.practice_session_runner

        def _run():
            try:
                runner(schedule, end)
            except Exception as exc:
                print(f"Practice session {schedule.get('name')} failed: {exc}")

        threading.Thread(target=_run, daemon=True,
                         name=f"practice-{schedule.get('name')}").start()
        return {"started": True, "name": schedule.get("name"),
                "window_minutes": 60}

    def handle_brief_get(self, conversation_id: str):
        return self._read_json_file(
            self.home / "chat_briefs" / f"{self._safe_name(conversation_id)}.json")

    def handle_brief_save(self, conversation_id: str, body: dict) -> dict:
        record = {"conversation_id": conversation_id, "brief": body.get("brief", body)}
        self._write_json_file(
            self._json_dir("chat_briefs") / f"{self._safe_name(conversation_id)}.json",
            record)
        return record

    def handle_mode_stop_delete(self, stop_id: str) -> dict:
        """Reject/abandon a stop: delete its record.

        The card stays in the thread as a rejected note; the stop
        itself is gone, so Proceed on it can no longer resume.
        """
        from .mode_stops import delete_mode_stop
        return {"ok": delete_mode_stop(self.home, stop_id)}

    def handle_mode_stop_latest(self, conversation_id: str) -> dict | None:
        """Newest pending Ask/Plan stop for a chat, spec/plan attached.

        The chat-open path renders the stop card from this, so the
        spec sheet survives a refresh: the card lives in the thread,
        not in a run record that dies with the page.
        """
        from .mode_stops import (enrich_mode_stop,
                                 latest_mode_stop_for_conversation)
        stop = latest_mode_stop_for_conversation(self.home, conversation_id)
        if stop is None:
            return None
        return enrich_mode_stop(self.home, stop)

    def handle_mode_stop_spec_save(self, stop_id: str, body: dict) -> dict:
        """Save an edited Ask spec sheet back to its stop.

        The sheet's Save button posts the edited fields; the stop
        record is updated, so a later Proceed resumes from the
        edited spec, and the specs/ JSON + Markdown copies are
        rewritten to match. An unverified spec still saves (the
        interview saves unverified specs too, so the user can see
        what is missing); the failures are reported, not hidden.
        """
        from .mode_stops import load_mode_stop, update_mode_stop_spec
        from ..agentic.interview import InterviewRunner, Spec, _parse_budget
        stop = load_mode_stop(self.home, stop_id)
        if stop is None:
            return {"ok": False, "error": "stop not found"}
        if stop.get("mode") != "ask":
            return {"ok": False, "error": "only Ask stops carry a spec"}
        data = body.get("spec", body) if isinstance(body, dict) else {}
        if not isinstance(data, dict):
            return {"ok": False, "error": "spec must be an object"}
        budget = data.get("budget_cents", data.get("budget"))
        if isinstance(budget, str):
            budget = _parse_budget(budget)
        elif isinstance(budget, (int, float)):
            budget = int(budget)
        else:
            budget = None
        spec = Spec.from_dict({**data, "budget_cents": budget})
        verified, failures = spec.verify()
        if not update_mode_stop_spec(self.home, stop_id, spec.to_dict()):
            return {"ok": False, "error": "stop not found"}
        conversation_id = str(stop.get("conversation_id") or "")
        if conversation_id:
            InterviewRunner(None, self.home,
                            conversation_id=conversation_id)._save_spec(spec)
        return {"ok": True, "verified": verified, "failures": failures,
                "spec": spec.to_dict()}

    def handle_ideas_list(self) -> dict:
        directory = self._json_dir("ideas")
        records = []
        for path in sorted(directory.glob("*.json")):
            data = self._read_json_file(path)
            if data is not None:
                records.append(data)
        return {"ideas": records}

    def handle_idea_create(self, body: dict) -> dict:
        import uuid
        idea_id = uuid.uuid4().hex[:12]
        record = {"id": idea_id, "text": str(body.get("text", ""))}
        self._write_json_file(self._json_dir("ideas") / f"{idea_id}.json", record)
        return record

    def handle_idea_delete(self, idea_id: str) -> dict:
        path = self.home / "ideas" / f"{self._safe_name(idea_id)}.json"
        if not path.is_file():
            return {"ok": False}
        path.unlink()
        return {"ok": True}

