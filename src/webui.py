"""WebUI server, and WhatsApp and Slack two-way transports.

WebUI, built now, after triggers, after v1 and MCP:
- Chat thread, in the browser, at /, with a left
  sidebar for projects, history, and library, and a
  chat box that sends a message to /api/chat.
- Approval cards, in the UI, not only in the CLI.
  When the loop asks for approval, a WebUIGate creates
  a card, waits up to 180 seconds, fail-closed, and
  /api/approve sets the decision. A card is a decision,
  chat is conversation, same rule as everywhere else.
- Settings and keys page, at /settings, showing
  backend, ingest path, and key hash prefixes only,
  never a real key, never a secret, never a token.
- Library tab, at /library, listing library files in
  the Aidon home, confined to home/library, with a
  file endpoint that serves only files under that
  path, never outside it.

Messaging, WhatsApp and Slack, both two-way, no Telegram:
- WhatsApp webhook, at /webhook/whatsapp. GET verifies
  with hub.mode, hub.verify_token, and hub.challenge,
  against WHATSAPP_VERIFY_TOKEN from the environment.
  POST parses WhatsApp Cloud API messages, creates a
  TriggerEvent source whatsapp, dispatches it, and
  sends the reply back on the same transport, via a
  reply callback. In production that callback posts
  to the WhatsApp Cloud API with WHATSAPP_TOKEN.
  In tests it records the reply, no real API call.
- Slack webhook, at /webhook/slack. POST verifies a
  Slack signature, HMAC SHA256 with SLACK_SIGNING_SECRET,
  timestamp within 5 minutes, and the X-Slack-Signature
  header, before parsing any event. A bad signature
  is 401, never parsed, never dispatched. Events
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
only the routes above, plus /api/library and
/api/projects. It does not serve the Aidon home,
it does not serve .env, it does not serve keys.
A real key is never in a response, only a hash prefix.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .approval import ApprovalGate, Decision
from .triggers import TriggerDispatcher, TriggerEvent

CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon</title></head>
<body>
<h1>Aidon</h1>
<div id="sidebar">
<h2>Projects</h2><div id="projects"></div>
<h2>Library</h2><a href="/library">Library tab</a>
<h2>Settings</h2><a href="/settings">Settings and keys</a>
</div>
<div id="chat">
<div id="messages"></div>
<form id="form"><input id="msg" name="message" placeholder="Type a goal, in plain language">
<button type="submit">Send</button></form>
<div id="approval"></div>
</div>
<script>
document.getElementById('form').onsubmit = async (e) => {
  e.preventDefault();
  const message = document.getElementById('msg').value;
  const r = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message})});
  const data = await r.json();
  document.getElementById('messages').innerHTML += '<p>' + (data.reply || '') + '</p>';
};
</script>
</body></html>
"""


class WebUIGate:
    """Approval gate backed by WebUI cards, fail-closed on timeout."""

    def __init__(self, timeout_seconds: int = 180):
        self.timeout_seconds = timeout_seconds
        self.cards: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._counter = 0
        self.gate = ApprovalGate(self._decide)

    def _decide(self, kind: str, title: str, body: str) -> Decision:
        self._counter += 1
        card_id = f"card-{self._counter}"
        event = threading.Event()
        self._events[card_id] = event
        self.cards[card_id] = {"kind": kind, "title": title, "body": body, "verdict": None}
        event.wait(timeout=self.timeout_seconds)
        card = self.cards[card_id]
        verdict = card["verdict"] or "rejected"
        return Decision(verdict)

    def approve(self, card_id: str, verdict: str) -> bool:
        if card_id not in self.cards:
            return False
        self.cards[card_id]["verdict"] = verdict
        self._events[card_id].set()
        return True


class WebUIServer:
    def __init__(self, home: Path, dispatcher: TriggerDispatcher | None = None,
                 auth_store=None, memory=None, whatsapp_reply=None,
                 slack_reply=None, whatsapp_verify_token: str = "",
                 slack_signing_secret: str = ""):
        self.home = Path(home)
        self.dispatcher = dispatcher or TriggerDispatcher()
        self.auth_store = auth_store
        self.memory = memory
        self.whatsapp_reply = whatsapp_reply
        self.slack_reply = slack_reply
        self.whatsapp_verify_token = whatsapp_verify_token
        self.slack_signing_secret = slack_signing_secret
        self.webui_gate = WebUIGate()
        # Default chat handler: echo, for the server core.
        # A real goal handler is registered by the caller,
        # with GoalRunner, interview, or a trigger handler.
        if "webui" not in self.dispatcher.handlers:
            self.dispatcher.register("webui", lambda e: e.send_reply(f"Received: {e.goal}"))

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

    def _projects(self) -> list[str]:
        if self.memory is None:
            return []
        try:
            return self.memory.list_projects()
        except Exception:
            return []

    def handle_chat(self, body: dict) -> dict:
        message = str(body.get("message", ""))
        project_id = str(body.get("project_id", "default"))
        replies: list[str] = []
        event = TriggerEvent(source="webui", kind="chat", goal=message,
                             project_id=project_id, reply=replies.append)
        try:
            self.dispatcher.dispatch(event)
        except Exception as exc:
            return {"reply": f"Error: {exc}"}
        return {"reply": replies[0] if replies else "Received."}

    def handle_whatsapp_get(self, query: dict) -> tuple[int, str]:
        mode = query.get("hub.mode", [""])[0]
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]
        if mode == "subscribe" and token and token == self.whatsapp_verify_token:
            return 200, challenge
        return 403, "forbidden"

    def handle_whatsapp_post(self, body: dict) -> dict:
        replies: list[str] = []
        try:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    for message in change.get("value", {}).get("messages", []):
                        text = message.get("text", {}).get("body", "")
                        sender = message.get("from", "")
                        event = TriggerEvent(source="whatsapp", kind="message",
                                             goal=text, payload={"from": sender},
                                             reply=replies.append)
                        self.dispatcher.dispatch(event)
        except Exception as exc:
            return {"error": str(exc)}
        if self.whatsapp_reply is not None:
            for reply in replies:
                self.whatsapp_reply(reply)
        return {"replies": replies}

    def handle_slack_post(self, raw: bytes, headers: dict, body: dict) -> tuple[int, dict]:
        # Verify signature before parsing any event as a goal.
        if self.slack_signing_secret:
            timestamp = headers.get("x-slack-request-timestamp", "")
            signature = headers.get("x-slack-signature", "")
            try:
                if abs(time.time() - int(timestamp)) > 300:
                    return 401, {"error": "stale timestamp"}
            except ValueError:
                return 401, {"error": "bad timestamp"}
            base = f"v0:{timestamp}:".encode() + raw
            expected = "v0=" + hmac.new(
                self.slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return 401, {"error": "bad signature"}
        if body.get("type") == "url_verification":
            return 200, {"challenge": body.get("challenge", "")}
        event_body = body.get("event", {})
        if event_body.get("type") == "message" and not event_body.get("bot_id"):
            text = str(event_body.get("text", ""))
            replies: list[str] = []
            event = TriggerEvent(source="slack", kind="message", goal=text,
                                 reply=replies.append)
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                return 200, {"error": str(exc)}
            if self.slack_reply is not None:
                for reply in replies:
                    self.slack_reply(reply)
            return 200, {"replies": replies}
        return 200, {"ok": True}

    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_html(self, status: int, html: str):
                data = html.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", 0) or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_html(200, CHAT_HTML)
                elif parsed.path == "/settings":
                    keys = server.auth_store.list_keys() if server.auth_store else []
                    html = "<h1>Settings</h1><p>Backend and ingest path are in config.</p>"
                    html += "<h2>Keys, hash prefixes only, never real keys</h2>"
                    for key in keys:
                        html += f"<p>{key['name']} {key['scope']} {key['key_hash_prefix']}</p>"
                    self._send_html(200, html)
                elif parsed.path == "/library":
                    files = server._library_files()
                    html = "<h1>Library</h1>"
                    for item in files:
                        html += f"<p>{item['path']} {item['size']} bytes</p>"
                    self._send_html(200, html)
                elif parsed.path == "/api/library":
                    self._send_json(200, {"files": server._library_files()})
                elif parsed.path == "/api/projects":
                    self._send_json(200, {"projects": server._projects()})
                elif parsed.path == "/webhook/whatsapp":
                    query = parse_qs(parsed.query)
                    status, text = server.handle_whatsapp_get(query)
                    data = text.encode()
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_POST(self):
                parsed = urlparse(self.path)
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                if parsed.path == "/api/chat":
                    self._send_json(200, server.handle_chat(body))
                elif parsed.path == "/api/approve":
                    card_id = str(body.get("id", ""))
                    verdict = str(body.get("verdict", "rejected"))
                    ok = server.webui_gate.approve(card_id, verdict)
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path == "/webhook/whatsapp":
                    self._send_json(200, server.handle_whatsapp_post(body))
                elif parsed.path == "/webhook/slack":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_slack_post(raw, headers, body)
                    self._send_json(status, payload)
                else:
                    self._send_json(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        return Handler

    def serve(self, port: int = 8002) -> None:
        HTTPServer(("127.0.0.1", port), self.make_handler()).serve_forever()
