"""WebUI server, and WhatsApp and Slack two-way transports.

WebUI, built now, after triggers, after v1 and MCP:
- Chat thread, in the browser, at /, with a left
  sidebar for history and library, and a
  chat box that sends a message to /api/chat.
- Approval cards, in the UI, not only in the CLI.
  When the loop asks for approval, a WebUIGate creates
  a card, waits up to 900 seconds, fail-closed, and
  /api/approve sets the decision. A card is a decision,
  chat is conversation, same rule as everywhere else.
- Settings APIs expose configuration and key metadata without exposing secrets.
  backend, ingest path, and key hash prefixes only,
  never a real key, never a secret, never a token.
- Library tab, at /library, listing library files in
  the Aidon home, confined to home/library, with a
  file endpoint that serves only files under that
  path, never outside it.

Messaging, WhatsApp and Slack, both two-way, no Telegram:
- WhatsApp webhook, at /webhook/whatsapp. GET verifies
  with hub.mode, hub.verify_token, and hub.challenge,
  against AIDON_WHATSAPP_VERIFY_TOKEN from the environment.
  POST parses WhatsApp Cloud API messages, creates a
  TriggerEvent source whatsapp, dispatches it, and
  sends the reply back on the same transport, via a
  reply callback. In production that callback posts
  to the WhatsApp Cloud API with WHATSAPP_TOKEN.
  In tests it records the reply, no real API call.
- Slack webhook, at /webhook/slack. POST verifies a
  Slack signature, HMAC SHA256 with AIDON_SLACK_SIGNING_SECRET,
  timestamp within 5 minutes, and the X-Slack-Signature
  header, before parsing any event. A bad signature
  is 401, never parsed, never dispatched. Without a
  configured signing secret every request is 401 (fail-closed). Events
  that are not user messages, for example url_verification
  and bot messages, are handled or ignored, never
  treated as a user goal. A user message creates a
  TriggerEvent source slack, dispatches it, and sends
  the reply back, via a reply callback, which in
  production posts to Slack with SLACK_BOT_TOKEN.
- Both transports use the same TriggerDispatcher as
  scheduler, WebUI, ingest, webhook, and manual, so
  a message, a WebUI chat, a scheduled job, and a
  file in the ingest folder all start a DAG run the
  same way, and none bypass approval, spec, or
  verification.
- No Telegram transport, by owner decision. There is
  no /webhook/telegram route, and a Telegram event
  is not routed to WhatsApp or Slack.

Server, stdlib only, localhost only:
The server binds 127.0.0.1, never 0.0.0.0, and serves
only the routes above, plus /api/library. It does not serve the Aidon home,
it does not serve .env, it does not serve keys.
A real key is never in a response, only a hash prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import math
import mimetypes
import os
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .approval import ApprovalGate, Decision
from .messaging import (MessagingConfig, normalize_number, parse_decision,
                        verify_whatsapp_signature)
from .result_delivery import extract_result_delivery
from .triggers import TriggerDispatcher, TriggerEvent
from .factory_page import FACTORY_HTML
from .webui_style import BASE_CSS
from .webui_settings import WebUISettingsMixin
from .webui_data import WebUIDataMixin
from .webui_capabilities import WebUICapabilityMixin
from .webui_workflows import WebUIWorkflowMixin
from .webui_messaging import WebUIMessagingMixin
from .webui_runs import WebUIRunMixin

STATIC_DIR = Path(__file__).parent / "static"

# Attachment limits are deliberately shared by the browser contract and the
# server enforcement.  The server is authoritative because clients can be
# replaced or bypassed.
MAX_ATTACHMENT_FILES = 3
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
MAX_ATTACHMENT_TOTAL_BYTES = MAX_ATTACHMENT_FILES * MAX_ATTACHMENT_BYTES


def _app_version() -> str:
    """Resolve the displayed release version without exposing repo details."""
    override = os.environ.get("AIDON_VERSION", "").strip().lstrip("v")
    if override:
        return override
    try:
        from importlib.metadata import version
        return version("aidon")
    except Exception:
        return "0.4.0"

# Shared page style: every WebUI page gets the same base look. The chat
# page previously shipped only avatar-animation CSS and the handler-built
# pages shipped no <head> at all, so they rendered as plain white text.
# No external assets: everything here is inline, so pages work offline.


def _page_html(title, body):
    """Wrap a handler-built fragment in a full styled page."""
    return ("<!doctype html><html data-theme=\"light\"><head><meta charset=\"utf-8\"><title>" + title +
            "</title><link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style><script src=\"/static/theme.js\"></script></head><body>" + body + "</body></html>")


from .webui_pages import CHAT_HTML, DAG_HTML, REPLAY_HTML, VRM_HTML


class WebUIGate:
    """Approval gate backed by WebUI cards, fail-closed on timeout.

    Cards and nudges live here; graduated autonomy lives on the
    inner ApprovalGate (same AutonomyPolicy / autonomy.json as CLI).

    - Each card carries its run context (run_id, conversation
      id, source, and a WhatsApp destination when one applies),
      so an approval is no longer an anonymous id: the card
      knows which run and conversation it belongs to.
    - On card creation, when the run's context names a
      WhatsApp destination — the run started on WhatsApp, or
      its WebUI/Slack conversation is WhatsApp-mirrored —
      the full card (title, body, id, and the approve/reject
      reply words) is sent to the owner on WhatsApp at once.
      The reply path already resolves "approve <id>" from any
      surface, so delivery was the missing half.
    - A nudge still goes to Slack via NudgeTracker, once per
      card, but it now fires before the card times out (the
      earlier of the configured nudge minutes and half the
      card timeout — a 15-minute nudge for a 900-second card
      could only ever arrive for an already-rejected card),
      carries the card title instead of a bare id, and is
      skipped when the card already has a verdict.
    - Autonomy: pass policy= into __init__ (or set
      self.gate.policy later). Callers use self.gate.ask(...,
      action_class=..., force_ask=...). High-stakes and
      force_ask paths always prompt; medium classes can
      auto-approve after the threshold. autonomy_tracker is
      optional legacy only — leave it None so counts are not
      double-recorded.
    """

    def __init__(self, timeout_seconds: int = 900, nudge_tracker=None,
                 autonomy_tracker=None, slack_sender=None, policy=None,
                 whatsapp_sender=None):
        self.timeout_seconds = timeout_seconds
        self.nudge_tracker = nudge_tracker
        self.autonomy_tracker = autonomy_tracker  # optional legacy
        self.slack_sender = slack_sender
        self.whatsapp_sender = whatsapp_sender
        self.cards: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._card_lock = threading.RLock()
        # Prefer shared AutonomyPolicy (same file as CLI)
        self.gate = ApprovalGate(self._decide, policy=policy)

    def _decide(self, kind: str, title: str, body: str,
                context: dict | None = None) -> Decision:
        import time as _time
        import uuid
        event = threading.Event()
        card_id = "card-" + uuid.uuid4().hex[:16]
        with self._card_lock:
            self._events[card_id] = event
            self.cards[card_id] = {
                "kind": kind, "title": title, "body": body, "verdict": None,
                "edited_text": "", "context": dict(context or {}),
            }
        pending_since = _time.time()
        slack_channel = (context or {}).get("slack_channel")
        if slack_channel and self.slack_sender is not None:
            snippet = body if len(body) <= 1000 else body[:1000] + "…"
            try:
                self.slack_sender(
                    slack_channel,
                    f"Approval needed [{card_id}]: {title}\n{snippet}\n"
                    f"Send in the channel: approve {card_id}  or  reject {card_id}",
                    (context or {}).get("slack_thread_ts"))
            except Exception:
                pass
        whatsapp_to = (context or {}).get("whatsapp_to")
        if whatsapp_to and self.whatsapp_sender is not None:
            snippet = body if len(body) <= 1000 else body[:1000] + "…"
            try:
                self.whatsapp_sender(
                    whatsapp_to,
                    f"Approval needed [{card_id}]: {title}\n{snippet}\n"
                    f"Reply: approve {card_id}  or  reject {card_id}")
            except Exception:
                pass
        if self.nudge_tracker is not None and self.nudge_tracker.nudge_minutes > 0:
            nudge_delay = min(self.nudge_tracker.nudge_minutes * 60,
                              self.timeout_seconds / 2)

            def _nudge():
                _time.sleep(max(0, nudge_delay))
                if not event.is_set() and \
                        self.cards[card_id]["verdict"] is None:
                    try:
                        self.nudge_tracker.check(
                            card_id, pending_since, _time.time(),
                            slack_sender=self.slack_sender, note=title,
                            min_elapsed_seconds=nudge_delay)
                    except Exception:
                        pass
            threading.Thread(target=_nudge, daemon=True).start()
        event.wait(timeout=self.timeout_seconds)
        with self._card_lock:
            card = self.cards[card_id]
            verdict = card["verdict"] or "rejected"
            card["verdict"] = verdict
            return Decision(verdict, edited_text=card.get("edited_text", ""))

    def pending_cards(self) -> list[dict]:
        """Cards still awaiting a verdict, in creation order.

        A card leaves this list as soon as it is approved or
        rejected, or when its wait times out (the timeout path
        writes the fail-closed verdict back into the card).
        """
        with self._card_lock:
            return [{"id": card_id, "kind": card["kind"],
                     "title": card["title"], "body": card["body"]}
                    for card_id, card in self.cards.items()
                    if card["verdict"] is None]

    def cancel_run(self, run_id: str) -> None:
        self.cancel_all(run_id=run_id)

    def cancel_all(self, run_id: str | None = None) -> None:
        """Reject pending cards, optionally only those belonging to one run."""
        with self._card_lock:
            for card_id, event in self._events.items():
                if run_id is not None and self.cards[card_id].get("context", {}).get("run_id") != run_id:
                    continue
                if not event.is_set():
                    self.cards[card_id]["verdict"] = "rejected"
                    event.set()

    def approve(self, card_id: str, verdict: str, edited_text: str = "") -> bool:
        with self._card_lock:
            card = self.cards.get(card_id)
            if card is None or card["verdict"] is not None:
                return False
            if verdict not in ("approved", "rejected", "edited"):
                return False
            card["verdict"] = verdict
            card["edited_text"] = str(edited_text or "")
            self._events[card_id].set()
            return True


HUB_MAX_EVENTS = 500




def _api_key_row(key: dict) -> str:
    """One API-key row of the settings page, escaped: key names are owner-supplied text."""
    name = html.escape(str(key.get("name", "")))
    scope = html.escape(str(key.get("scope", "")))
    prefix = html.escape(str(key.get("key_hash_prefix", "")))
    return (f"<div class=\"set-row\"><div class=\"set-label\"><label>{name}</label>"
            f"<span class=\"set-key\">{scope}</span></div><div class=\"set-input\">"
            f"<span class=\"set-key\">hash {prefix}</span></div></div>")


class WebUIServer(WebUIRunMixin, WebUIMessagingMixin, WebUISettingsMixin, WebUIDataMixin, WebUICapabilityMixin,
                  WebUIWorkflowMixin):
    def __init__(self, home: Path, dispatcher: TriggerDispatcher | None = None,
                 auth_store=None, memory=None, whatsapp_reply=None,
                 slack_reply=None, whatsapp_verify_token: str = "",
                 slack_signing_secret: str = "", messaging_config=None,
                 whatsapp_sender=None, slack_sender=None, webhook_only: bool = False,
                 media_downloader=None, run_control=None, webui_token=None, transcribe_key=None,
                 tts_key=None, config=None):
        self.home = Path(home)
        self.config = config
        self.dispatcher = dispatcher or TriggerDispatcher()
        self.auth_store = auth_store
        self.memory = memory
        # whatsapp_sender(to, text) and slack_sender(channel, text, thread_ts)
        # are the two-way senders. whatsapp_reply / slack_reply are
        # kept only as a single-argument fallback, for tests.
        self.whatsapp_sender = whatsapp_sender
        if self.whatsapp_sender is None and whatsapp_reply is not None:
            self.whatsapp_sender = lambda to, text: whatsapp_reply(text)
        self.slack_sender = slack_sender
        if self.slack_sender is None and slack_reply is not None:
            self.slack_sender = lambda channel, text, thread_ts=None: slack_reply(text)
        self.messaging_config = messaging_config or MessagingConfig()
        # Old key names win. A value passed explicitly here
        # overrides the config, which is how tests inject it.
        self.whatsapp_verify_token = (
            whatsapp_verify_token or self.messaging_config.whatsapp_verify_token)
        self.slack_signing_secret = (
            slack_signing_secret or self.messaging_config.slack_signing_secret)
        # Approval timeout comes from config when present, so
        # AIDON_APPROVAL_TIMEOUT_S actually changes the gate;
        # without a config the gate keeps its 900s default.
        timeout_seconds = getattr(config, "approval_timeout_seconds", 900)
        if timeout_seconds is None:
            timeout_seconds = 900
        self.webui_gate = WebUIGate(timeout_seconds=timeout_seconds)
        self.webhook_only = webhook_only
        self.media_downloader = media_downloader
        self.run_control = run_control
        self._web_runs: dict[str, dict] = {}
        # Set by the serve daemon: practice_session_runner(schedule, end)
        # runs one practice session synchronously (report delivery and
        # session recording included). None without the daemon, so the
        # Practice page's run-now honestly reports it is unavailable.
        self.practice_session_runner = None
        # Runs blocked in ask_user, by run id and by conversation id.
        # A question is a chat turn, not an approval card: no clock.
        self._question_waiters: dict[str, dict] = {}
        self._question_by_conversation: dict[str, dict] = {}
        self._question_lock = threading.Lock()
        self.webui_token = webui_token
        self.webui_allowed_hosts = getattr(config, "webui_allowed_hosts", "") or ""
        self.transcribe_key = transcribe_key
        # Live realtime transcription sessions, by id: the browser
        # sends PCM chunks and reads partials over HTTP, while the
        # server holds the Meta realtime WebSocket (and the key).
        self.realtime_sessions: dict = {}
        self.tts_key = tts_key
        self.tts_voice_id = str(getattr(config, "tts_voice_id", "") or "")
        # Default chat handler: echo, for the server core. A real
        # goal handler is registered by the caller, with GoalRunner,
        # interview, or a trigger handler. This used to sit after an
        # unconditional return in _resolve_decision, so it never ran
        # and a bare server answered every chat with "no handler".
        if "webui" not in self.dispatcher.handlers:
            self.dispatcher.register(
                "webui", lambda e: e.send_reply(f"Received: {e.goal}"))

    def _resolve_decision(self, text: str) -> str | None:
        decision = parse_decision(text)
        if not decision:
            return None
        verb, card_id = decision
        verdict = "approved" if verb == "approve" else "rejected"
        ok = self.webui_gate.approve(card_id, verdict)
        if ok:
            return f"{verdict.capitalize()} {card_id}"
        return f"No such approval {card_id}"

    def _resolve_control(self, text: str) -> str | None:
        """Steer and cancel, from chat or a message, while a run is running.

        Commands, all surfaces, same run id:
        - cancel <run_id> or stop <run_id>: stop the run, no further steps.
        - steer <run_id> <note>: add a note for the next repair attempt.
        Returns a reply, or None when the text is not a control command.
        """
        if self.run_control is None:
            return None
        parts = text.strip().split(None, 2)
        if len(parts) >= 2 and parts[0].lower() in ("cancel", "stop"):
            ok = self.run_control.cancel(parts[1], gate=self.webui_gate)
            return f"Cancelled {parts[1]}" if ok else f"No such run {parts[1]}"
        if len(parts) >= 3 and parts[0].lower() == "steer":
            ok = self.run_control.steer(parts[1], parts[2])
            return f"Steered {parts[1]}: {parts[2]}" if ok else f"No such run {parts[1]}"
        return None

    # ── pre-approval, one store, three surfaces ──────────────────────
    def preapproval_store(self):
        from .preapproval import PreapprovalStore
        return PreapprovalStore(self.home)

    def _resolve_preapproval(self, text: str) -> str | None:
        """Handle preapprove / preapprovals / unpreapprove from any chat.

        Returns None when the text is not a pre-approval command, so the
        caller can carry on and treat it as a goal. The same function
        serves the WebUI chat box, WhatsApp, and Slack, so a grant made
        on a phone is the same grant the WebUI panel shows.
        """
        from .preapproval import handle_command
        try:
            return handle_command(self.preapproval_store(), text)
        except Exception as exc:
            return f"Pre-approval error: {type(exc).__name__}: {exc}"

    def _resolve_memory_command(self, text: str) -> str | None:
        """Handle explicit memory pinning from every messaging surface."""
        stripped = text.strip()
        if stripped == "/remember":
            return "Usage: /remember <fact or preference>"
        if not stripped.lower().startswith("/remember "):
            return None
        fact = stripped[len("/remember"):].strip()
        if not fact:
            return "Usage: /remember <fact or preference>"
        if self.memory is None:
            return "Long-term memory is unavailable."
        try:
            point = self.memory.remember(fact, source="chat-command")
            return f"Remembered in long-term memory: {fact} (id {point.id})"
        except Exception as exc:
            return f"Could not remember that: {exc}"

    @staticmethod
    def _force_web_prefix(text: str) -> tuple[str, bool]:
        """Strip the explicit /web command and return its force flag."""
        stripped = str(text or "").strip()
        if stripped == "/web":
            return "", True
        if stripped.lower().startswith("/web "):
            return stripped[5:].strip(), True
        return stripped, False

    @staticmethod
    def _merge_correction_followup(text: str, history: list[dict]) -> str:
        """Attach a terse "I meant ..." correction to its prior question."""
        import re

        correction = re.match(
            r"^(?:sorry[,\s]+)?(?:i meant|i mean|i was referring to|"
            r"correction(?:\s+to that)?\s*:)\s*(.+)$",
            str(text or "").strip(), re.IGNORECASE | re.DOTALL)
        if correction is None:
            return str(text or "")
        previous = next((str(item.get("text", "")).strip()
                          for item in reversed(history or [])
                          if item.get("role") == "user"
                          and str(item.get("text", "")).strip()
                          and not str(item.get("text", "")).lstrip().startswith("/")), "")
        if not previous:
            return str(text or "")
        return (f"{previous}\n\nCorrection to the previous question: "
                f"{correction.group(1).strip()}. Apply this correction to "
                "the previous question and answer that corrected question; "
                "do not treat the correction fragment as a separate topic.")

    def handle_preapprovals(self) -> dict:
        """Active grants, the kind menu, and the help text, for the UI."""
        from .preapproval import HELP_TEXT, KINDS
        try:
            grants = self.preapproval_store().active()
        except Exception:
            grants = []
        return {"grants": grants, "kinds": KINDS, "help": HELP_TEXT}

    def handle_preapproval_grant(self, body: dict) -> tuple[int, dict]:
        """Grant from the WebUI form: conversation, kind (name or number), hours."""
        import time as _time

        from .preapproval import (DEFAULT_HOURS, IRREVERSIBLE, KIND_NAMES,
                                  MAX_HOURS, grantable_kind, kind_from_token)
        spec_id = str(body.get("spec_id") or body.get("conversation_id")
                      or "").strip()
        kind = kind_from_token(str(body.get("kind", "")))
        if not spec_id:
            return 400, {"ok": False, "error": "conversation id is required"}
        if kind is None:
            return 400, {"ok": False, "error": "choose a kind, by number or name"}
        if kind in IRREVERSIBLE:
            return 400, {"ok": False,
                         "error": f"{kind} can never be pre-approved: it is "
                                  f"irreversible or leaves the machine"}
        if not grantable_kind(kind):
            return 400, {"ok": False,
                         "error": f"unknown kind {kind!r}, choose one of: "
                                  f"{', '.join(KIND_NAMES)}"}
        try:
            hours = float(body.get("hours") or DEFAULT_HOURS)
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "hours must be a number"}
        if hours <= 0:
            return 400, {"ok": False, "error": "hours must be greater than 0"}
        hours = min(hours, MAX_HOURS)
        ok = self.preapproval_store().grant(
            spec_id, kind, _time.time() + hours * 3600.0)
        if not ok:
            return 400, {"ok": False, "error": "grant refused"}
        return 200, {"ok": True, "spec_id": spec_id, "kind": kind,
                     "hours": hours}

    def handle_preapproval_revoke(self, spec_id: str,
                                  kind: str = "") -> tuple[int, dict]:
        from .preapproval import kind_from_token
        spec_id = str(spec_id or "").strip()
        if not spec_id:
            return 400, {"ok": False, "error": "conversation id is required"}
        resolved = kind_from_token(kind) if kind else None
        if kind and resolved is None:
            return 400, {"ok": False, "error": f"unknown kind {kind!r}"}
        removed = self.preapproval_store().revoke(spec_id, resolved)
        return (200, {"ok": True, "removed": removed}) if removed \
            else (404, {"ok": False, "removed": 0, "error": "nothing to revoke"})

    def _library_files(self) -> list[dict]:
        library_dir = self.home / "library" / "files"
        if not library_dir.exists():
            return []
        out = []
        for path in sorted(library_dir.rglob("*")):
            if path.is_file():
                out.append({"name": path.name, "path": str(path.relative_to(library_dir)),
                            "size": path.stat().st_size})
        return out

    def _avatar_path(self) -> Path | None:
        import os
        candidates = []
        configured = os.environ.get("AIDON_VRM_PATH", "").strip()
        if not configured and "AIDON_VRM_PATH" in os.environ:
            # Explicitly blank = the avatar is off. An unset variable
            # still falls through to the drop-in locations below, so a
            # fresh install keeps working without any config edit.
            return None
        if configured:
            candidates.append(Path(configured).expanduser())
        candidates.extend((self.home / "avatar.vrm",
                           self.home / "profile" / "avatar.vrm"))
        return next((path for path in candidates if path.is_file()), None)




    def handle_memory_edit(self, collection: str, point_id: str, text: str) -> dict:
        """Edit a memory point's text, or record the edit if the
        point is not in the live store. Never silently dropped:
        when the live store has no such point, the edit is written
        to <home>/memory_edits/<point_id>.json and the result says
        so, so it can be applied later instead of vanishing."""
        collection = str(collection or "episodic")
        point_id = str(point_id or "")
        text = str(text or "")
        if not point_id or not text:
            return {"ok": False, "error": "memory edit needs point_id and text"}
        if self.memory is not None:
            try:
                store = getattr(self.memory, "store", None)
                point = None
                if store is not None and hasattr(store, "get"):
                    point = store.get(collection, point_id)
                if point is not None:
                    point.text = text
                    if isinstance(getattr(point, "payload", None), dict):
                        point.payload["text"] = text
                    if hasattr(store, "set_payload"):
                        store.set_payload(collection, point_id, {"text": text})
                    if hasattr(store, "upsert"):
                        store.upsert(collection, point)
                    return {"ok": True, "point_id": point_id, "collection": collection, "via": "store"}
            except Exception as exc:
                return self._record_memory_edit(collection, point_id, text, error=f"{type(exc).__name__}: {exc}")
        return self._record_memory_edit(collection, point_id, text)

    def _record_memory_edit(self, collection: str, point_id: str, text: str, error: str = "") -> dict:
        record = {"collection": collection, "point_id": point_id, "text": text, "action": "edit"}
        if error:
            record["store_error"] = error
        self._write_json_file(
            self._json_dir("memory_edits") / f"{self._safe_name(point_id)}.json", record)
        return {"ok": True, "point_id": point_id, "collection": collection, "via": "recorded", "recorded": True}

    def handle_skill_edit(self, name: str, description: str) -> dict:
        """Edit a skill's description in the SkillLibrary, or record
        the edit if the skill is not in the library. Never silently
        dropped: a missing skill gets a <home>/skill_edits/<name>.json
        record instead of a vanishing edit."""
        name = str(name or "")
        description = str(description or "")
        if not name or not description:
            return {"ok": False, "error": "skill edit needs name and description"}
        try:
            from ..capabilities.skill_library import SkillLibrary
            library = SkillLibrary(self.home)
            skill = library.get(name)
            if skill is not None:
                skill.description = description
                library._update(skill)
                return {"ok": True, "name": name, "via": "library"}
        except Exception as exc:
            record = {"name": name, "description": description, "action": "edit", "library_error": f"{type(exc).__name__}: {exc}"}
            self._write_json_file(
                self._json_dir("skill_edits") / f"{self._safe_name(name)}.json", record)
            return {"ok": True, "name": name, "via": "recorded", "recorded": True}
        record = {"name": name, "description": description, "action": "edit"}
        self._write_json_file(
            self._json_dir("skill_edits") / f"{self._safe_name(name)}.json", record)
        return {"ok": True, "name": name, "via": "recorded", "recorded": True}

    def handle_plugin_adopt(self, path: str, approve=None) -> dict:
        """Adopt a plugin directory through the approval gate.

        When approve is supplied (tests, or a caller that already
        holds approval) it is passed straight to adopt. Otherwise
        adoption goes through the server's approval gate, which
        creates a card the owner approves or rejects; with no gate
        available adoption is refused rather than auto-adopted.
        Findings are returned as plain dicts, never raw objects.
        """
        path = str(path or "")
        if not path:
            return {"ok": False, "error": "plugin adopt needs a path"}
        source = Path(path)
        if not source.is_dir():
            return {"ok": False, "error": f"plugin directory not found: {path}"}
        if self.config is None:
            return {"ok": False,
                    "error": "plugin adoption requires a configured isolated acceptance-test backend"}
        from ..capabilities.plugins import PluginRegistry
        from ..capabilities.plugin_acceptance import acceptance_backend
        registry = PluginRegistry(self.home)
        registry.load_all()
        gate = None if approve is not None else getattr(self.webui_gate, "gate", None)
        if approve is None and gate is None:
            return {"ok": False, "error": "adoption needs approval"}
        ok, findings, message = registry.adopt(
            source, gate=gate, approve=approve,
            test_backend_factory=(lambda: acceptance_backend(self.config, self.home / "workspace")) if self.config is not None else None,
            tool_posture=str(getattr(self.config, "tool_posture", "standard")
                              or "standard").strip().lower(),
            kernel_mode=bool(getattr(self.config, "kernel_mode", False)))
        finding_dicts = []
        for finding in findings:
            if hasattr(finding, "to_dict"):
                finding_dicts.append(finding.to_dict())
            else:
                finding_dicts.append({"type": getattr(finding, "type", ""), "path": str(getattr(finding, "path", "")),
                                      "line": getattr(finding, "line", 0), "message": str(getattr(finding, "message", finding))})
        return {"ok": ok, "findings": finding_dicts, "message": message}

    def handle_entities(self, entity: str) -> dict:
        graph = getattr(self.memory, "entity_graph", None) if self.memory is not None else None
        if graph is None:
            return {"entity": entity, "related": []}
        return {"entity": entity, "related": graph.points_for(entity)}

    def handle_memory_graph(self, collections=None, limit: int = 500) -> dict:
        """Return a graph-ready view of memory and KB points.

        Point nodes connect to shared entity nodes. The same entity graph is
        used for episodic memory and knowledge-base records, so cross-links
        are visible without inventing relationships with another model call.
        """
        if self.memory is None:
            return {"nodes": [], "edges": [], "error": "memory unavailable"}
        wanted = set(collections or ("working", "episodic", "semantic", "experience", "knowledge"))
        nodes = []
        edges = []
        seen_entities = set()
        count = 0
        for collection in wanted:
            if collection not in {"working", "episodic", "semantic", "experience", "knowledge"}:
                continue
            for point in self.memory.store.iter_points(collection):
                if count >= max(1, min(int(limit), 2000)):
                    break
                point_id = str(point.id)
                node_id = f"point:{collection}:{point_id}"
                nodes.append({"id": node_id, "type": "memory", "collection": collection,
                              "label": str(point.text or "")[:100]})
                graph = getattr(self.memory, "entity_graph", None)
                entities = graph.entities_for_point(collection, point_id) if graph else []
                for entity in entities:
                    entity_id = f"entity:{entity}"
                    if entity not in seen_entities:
                        nodes.append({"id": entity_id, "type": "entity", "label": entity})
                        seen_entities.add(entity)
                    edges.append({"source": node_id, "target": entity_id})
                count += 1
            if count >= max(1, min(int(limit), 2000)):
                break
        return {"nodes": nodes, "edges": edges}

    # -- Additional WebUI surfaces: hub runs, reports, TTS, and goals with images. --
    def _hub_run_path(self, run_id: str) -> Path:
        return self.home / "hub_runs" / f"{self._safe_name(run_id)}.json"

    def handle_hub_run_create(self, body: dict) -> dict:
        run_id = str(body.get("id", "") or body.get("run_id", ""))
        if not run_id:
            return {"ok": False, "error": "run needs an id"}
        record = {"id": run_id, "goal": str(body.get("goal", "")),
                  "events": [], "stages": [], "traces": []}
        self._write_json_file(self._hub_run_path(run_id), record)
        return {"ok": True, **record}

    def _hub_run_append(self, run_id: str, key: str, entry: dict) -> dict:
        path = self._hub_run_path(run_id)
        record = self._read_json_file(path)
        if record is None:
            record = {"id": run_id, "goal": "", "events": [], "stages": [], "traces": []}
        record.setdefault(key, []).append(entry)
        if len(record[key]) > HUB_MAX_EVENTS:
            record[key] = record[key][-HUB_MAX_EVENTS:]
        self._write_json_file(path, record)
        return {"ok": True, "id": run_id}

    def handle_hub_run_event(self, run_id: str, body: dict) -> dict:
        return self._hub_run_append(run_id, "events", body)

    def handle_hub_run_stage(self, run_id: str, body: dict) -> dict:
        return self._hub_run_append(run_id, "stages", body)

    def handle_hub_run_trace(self, run_id: str, body: dict) -> dict:
        return self._hub_run_append(run_id, "traces", body)

    @staticmethod
    def _elide_base64(value):
        """Return a copy of value with long base64 strings elided.

        A string longer than 1000 chars, made only of base64
        characters, with a length that is a multiple of 4, is
        replaced with "<elided:base64>". Short strings and
        non-base64 strings are untouched. Dicts and lists are
        recursed into; the caller's structure is never mutated.
        """
        import re
        if isinstance(value, dict):
            return {k: WebUIServer._elide_base64(v) for k, v in value.items()}
        if isinstance(value, list):
            return [WebUIServer._elide_base64(v) for v in value]
        if isinstance(value, str) and len(value) > 1000 and len(value) % 4 == 0 \
                and re.fullmatch(r"[A-Za-z0-9+/=]+", value):
            return "<elided:base64>"
        return value

    def finish_hub_run(self, run_id: str) -> dict:
        path = self._hub_run_path(run_id)
        record = self._read_json_file(path)
        if record is None:
            return {"ok": False, "error": "run not found"}
        record["status"] = "done"
        for key in ("events", "stages", "traces"):
            if key in record:
                record[key] = self._elide_base64(record[key])
        self._write_json_file(path, record)
        return {"ok": True, "id": run_id, "status": "done"}

    def handle_report_generate(self, body: dict) -> dict:
        day = self._safe_name(body.get("day", "")) or "report"
        summary = str(body.get("summary", ""))
        path = self._json_dir("reports") / f"{day}.md"
        path.write_text(f"# Report {day}\n\n{summary}\n", encoding="utf-8")
        return {"ok": True, "day": day, "path": str(path)}

    def handle_tts(self, body: dict) -> tuple[int, dict]:
        tts_key = getattr(self, "tts_key", None)
        if not tts_key:
            return 501, {"error": "TTS key not configured"}
        from ..capabilities.voice import tts as _tts
        import re
        text = str(body.get("text", "")).strip()
        if not text:
            return 400, {"error": "empty text"}
        text = text[:2000]
        text = re.sub(r"[#>*`_\[\]()|]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        voice_id = str(body.get("voice_id", "") or self.tts_voice_id)
        result = _tts(text, tts_key, voice_id)
        if not result.get("ok"):
            return 500, {"error": result.get("error", "TTS failed")}
        return 200, {"ok": True,
                     "audio_bytes": len(result.get("audio", b"")),
                     "_audio": result.get("audio", b"")}

    def open_tts_stream(self, body: dict):
        """Open a progressive Fish response for the WebUI audio player."""
        import re
        text = str(body.get("text", "")).strip()
        if not getattr(self, "tts_key", None):
            raise ValueError("TTS key not configured")
        if not text:
            raise ValueError("empty text")
        text = text[:2000]
        text = re.sub(r"[#>*`_\[\]()|]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        from ..capabilities.voice import open_tts_stream as _open_tts_stream
        return _open_tts_stream(text, self.tts_key,
                                str(body.get("voice_id", "") or self.tts_voice_id))

    @staticmethod
    def parse_goal_multipart(raw: bytes, content_type: str) -> tuple[dict, list[dict]]:
        """Parse a goal form without decoding the binary file parts as JSON."""
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("expected multipart/form-data")
        header = (b"Content-Type: " + content_type.encode("ascii", "ignore") +
                  b"\r\nMIME-Version: 1.0\r\n\r\n")
        message = BytesParser(policy=email_default).parsebytes(header + raw)
        fields: dict[str, str] = {}
        files: list[dict] = []
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            data = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                files.append({"name": filename, "data": data,
                              "mime": part.get_content_type()})
            else:
                fields[str(name)] = data.decode("utf-8", "replace")
        return fields, files

    def handle_goal_with_files(self, body: dict, files: list[dict]) -> dict:
        """Store attachments, start the goal, and attach images to inference."""
        if len(files) > MAX_ATTACHMENT_FILES:
            return {"error": f"at most {MAX_ATTACHMENT_FILES} files per request"}
        total = sum(len(item.get("data", b"")) for item in files)
        if total > MAX_ATTACHMENT_TOTAL_BYTES:
            return {"error": f"attachments exceed the {MAX_ATTACHMENT_TOTAL_BYTES // (1024 * 1024)} MiB total limit"}
        upload_dir = self.home / "library" / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)
        image_paths: list[str] = []
        document_paths: list[str] = []
        video_paths: list[str] = []
        for item in files:
            data = item.get("data", b"")
            if len(data) > MAX_ATTACHMENT_BYTES:
                return {"error": f"{item.get('name', 'attachment')} exceeds the {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB per-file limit"}
            name = self._safe_name(item.get("name", "attachment"))
            mime = str(item.get("mime", "application/octet-stream")).lower()
            digest = hashlib.sha256(data).hexdigest()[:12]
            destination = upload_dir / f"{digest}-{name}"
            destination.write_bytes(data)
            if mime.startswith("video/") or name.lower().endswith((".mp4", ".webm", ".mov", ".m4v", ".avi")):
                video_paths.append(str(destination))
            elif mime.startswith("image/") or name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                image_paths.append(str(destination))
            else:
                document_paths.append(str(destination))
        goal = str(body.get("goal", "")).strip()
        if not goal:
            return {"error": "goal is required"}
        if document_paths:
            goal += "\n\nAttached document paths (use the document reader when needed):\n" + "\n".join(document_paths)
        if video_paths:
            goal += "\n\nAttached video paths (use the video workflow when needed):\n" + "\n".join(video_paths)
        if image_paths:
            goal += ("\n\nAttached image paths (available to image-aware "
                     "sandbox plugins after approval):\n" + "\n".join(image_paths))
        result = self.start_goal({**body, "goal": goal, "image_paths": image_paths})
        result.update({"images": [Path(path).name for path in image_paths],
                      "documents": [Path(path).name for path in document_paths],
                      "videos": [Path(path).name for path in video_paths]})
        return result

    def handle_goal_with_images(self, body: dict) -> dict:
        """Compatibility JSON form for API callers that already have base64."""
        import base64 as _b64
        files = []
        for image in body.get("images", []) or []:
            try:
                data = _b64.b64decode(str(image.get("data_b64", "")), validate=True)
            except Exception:
                return {"error": "bad base64 attachment"}
            files.append({"name": image.get("name", "image.png"),
                          "data": data, "mime": mimetypes.guess_type(
                              str(image.get("name", "image.png")))[0] or "image/png"})
        return self.handle_goal_with_files(body, files)

    # -- WebUI routes -----------------------------------------------------
    # Each handler below is pure server logic (no socket), so tests
    # can call it directly; the do_GET/do_POST/do_DELETE branches
    # only map paths and status codes onto these results. Auth is
    # the same is_authorized check the other /api/ routes use, run
    # at the top of each do_* method.

    def handle_keys(self) -> dict:
        """API keys as name, scope, and hash prefix only.

        The static settings page used to lose this list when the page
        moved out of the inline HTML, so it is served here: never a
        real key, only the first characters of the hash, which is
        enough for the owner to tell two keys apart.
        """
        try:
            keys = self.auth_store.list_keys() if self.auth_store else []
        except Exception:
            keys = []
        return {"keys": keys}

    def handle_voice(self) -> dict:
        """Voice capability info: whether spoken replies are
        announced, and whether a TTS key is configured. No secret
        is ever returned, only booleans.
        """
        return {"announce": bool(getattr(self, "voice_announce", False)),
                "tts_available": bool(getattr(self, "tts_key", None))}

    def handle_status(self) -> dict:
        """Return safe, human-readable capability status for the dashboard."""
        import os
        config = self.config
        model = str(getattr(config, "model", "") or os.environ.get(
            "AIDON_MODEL", "muse-spark"))
        backend = str(getattr(config, "backend", "") or "auto")
        memory = self.memory
        memory_warning = ""
        if getattr(memory, "_memory", None) is not None:
            store_name = type(memory._memory.store).__name__
            memory_name = f"{type(memory._memory.embedder).__name__} · {store_name}"
            warnings = []
            if store_name == "SQLiteStore":
                warnings.append("Memory is on the SQLite fallback, not Qdrant — "
                                "run ./setup.sh --with-qdrant for shared vector memory.")
            if type(memory._memory.embedder).__name__ == "HashEmbedder":
                warnings.append("Memory uses hash embeddings, not semantic embeddings — "
                                "install the embedding packages with ./setup.sh for real recall.")
            memory_warning = " ".join(warnings)
        elif os.environ.get("AIDON_EMBEDDER", "").strip().lower() == "hash":
            memory_name = "Hash memory"
            memory_warning = ("Memory uses hash embeddings, not semantic embeddings — "
                              "install the embedding packages with ./setup.sh for real recall.")
        else:
            try:
                import importlib.util
                if importlib.util.find_spec("qdrant_client") is None:
                    memory_name = "SQLite memory (persistent fallback)"
                    memory_warning = ("Memory is on the SQLite fallback, not Qdrant — "
                                      "run ./setup.sh --with-qdrant for shared vector memory.")
                else:
                    memory_name = "Semantic memory (on demand)"
            except Exception:
                memory_name = "SQLite memory (persistent fallback)"
                memory_warning = ("Memory is on the SQLite fallback, not Qdrant — "
                                  "run ./setup.sh --with-qdrant for shared vector memory.")
        msg = self.messaging_config
        sandbox = str(getattr(config, "execution_backend", "bubblewrap"))
        computer = str(getattr(config, "computer_driver", "") or "off")
        return {
            "model": model,
            "backend": backend,
            "memory": memory_name,
            "memory_warning": memory_warning,
            "whatsapp": "Online" if msg.whatsapp_configured() else "Not configured",
            "slack": "Online" if msg.slack_configured() else "Not configured",
            "web_search": str(getattr(config, "search_backend", "tavily")),
            "sandbox": sandbox,
            "voice": "Ready" if self.tts_key or self.transcribe_key else "Not configured",
            "computer": computer,
        }


    def make_handler(self):
        from .webui_http import make_handler
        return make_handler(self)


    def serve(self, port: int = 8002, host: str = "127.0.0.1") -> None:
        # Threading, so a goal run waiting on an approval card
        # does not block the /api/approve request that resolves it.
        ThreadingHTTPServer((host, port), self.make_handler()).serve_forever()
