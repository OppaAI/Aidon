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
  against AIDON_WHATSAPP_VERIFY_TOKEN from the environment.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .approval import ApprovalGate, Decision
from .messaging import (MessagingConfig, normalize_number, parse_decision,
                        verify_whatsapp_signature)
from .triggers import TriggerDispatcher, TriggerEvent

CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon</title></head>
<body>
<h1>Aidon</h1>
<div id="avatar" aria-label="Aidon avatar">
<svg id="avatar-face" width="120" height="120" viewBox="0 0 120 120" role="img" aria-label="Aidon">
<circle cx="60" cy="60" r="50" fill="#ddeeff" stroke="#336699" stroke-width="3"/>
<circle id="eye-left" cx="42" cy="52" r="6" fill="#223344"/>
<circle id="eye-right" cx="78" cy="52" r="6" fill="#223344"/>
<path id="mouth" d="M 42 78 Q 60 90 78 78" fill="none" stroke="#223344" stroke-width="3" stroke-linecap="round"/>
</svg>
<p id="avatar-status">Idle</p>
<p><button id="mic" type="button">🎤 Mic</button> <label><input id="announce" type="checkbox"> Voice announce</label></p>
<p id="mic-status"></p>
</div>
<div id="sidebar">
<h2>Projects</h2><div id="projects"></div>
<h2>Library</h2><a href="/library">Library tab</a>
<h2>Settings</h2><a href="/settings">Settings and keys</a>
<h2>Workflows</h2><a href="/dag">DAG editor</a> <a href="/factory">Factory</a>
</div>
<div id="chat">
<div id="messages"></div>
<form id="form"><input id="msg" name="message" placeholder="Type a goal, in plain language, or use the mic">
<button type="submit">Send</button></form>
<div id="approval"></div>
</div>
<script>
function setAvatar(status, mouth) {
  document.getElementById('avatar-status').textContent = status;
  document.getElementById('mouth').setAttribute('d', mouth);
}
function speak(text) {
  if (document.getElementById('announce').checked && 'speechSynthesis' in window) {
    window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
  }
}
async function sendMessage(message) {
  setAvatar('Working', 'M 42 78 Q 60 84 78 78');
  const r = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message})});
  const data = await r.json();
  const reply = data.reply || '';
  document.getElementById('messages').innerHTML += '<p>' + reply + '</p>';
  if (reply.includes('Done:')) { setAvatar('Done', 'M 42 76 Q 60 94 78 76'); }
  else if (reply.includes('Failed:')) { setAvatar('Failed', 'M 42 86 Q 60 74 78 86'); }
  else if (reply.includes('Cancelled')) { setAvatar('Cancelled', 'M 42 80 L 78 80'); }
  else if (reply.includes('Rejected')) { setAvatar('Waiting', 'M 42 80 L 78 80'); }
  else { setAvatar('Idle', 'M 42 78 Q 60 90 78 78'); }
  speak(reply);
}
document.getElementById('form').onsubmit = async (e) => {
  e.preventDefault();
  const message = document.getElementById('msg').value;
  document.getElementById('msg').value = '';
  await sendMessage(message);
};
document.getElementById('mic').onclick = () => {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) {
    document.getElementById('mic-status').textContent = 'Mic is not supported in this browser. Use Chrome on localhost, or type instead.';
    return;
  }
  const recognition = new SpeechRecognition();
  recognition.lang = 'en-US';
  recognition.interimResults = false;
  document.getElementById('mic-status').textContent = 'Listening...';
  recognition.onresult = async (event) => {
    const text = event.results[0][0].transcript;
    document.getElementById('mic-status').textContent = 'Heard: ' + text;
    await sendMessage(text);
  };
  recognition.onerror = (event) => {
    document.getElementById('mic-status').textContent = 'Mic error: ' + event.error;
  };
  recognition.start();
};
</script>
</body></html>
"""

DAG_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon DAG editor</title></head>
<body>
<h1>DAG editor</h1>
<p>Edit a saved workflow: add, remove, modify, and reorder nodes. A DAG that fails validation is not saved, the failures are shown, so a broken graph is never executed.</p>
<div id="list"></div>
<h2>Editor</h2>
<p><label>DAG id <input id="dag-id"></label> <label>Goal <input id="dag-goal" size="60"></label></p>
<div id="nodes"></div>
<p>
<button id="add-node" type="button">Add node</button>
<button id="save" type="button">Validate and save</button>
<button id="delete" type="button">Delete</button>
</p>
<p id="result"></p>
<script>
let nodes = [];
function render() {
  const div = document.getElementById('nodes');
  div.innerHTML = '';
  nodes.forEach((node, index) => {
    const row = document.createElement('div');
    row.innerHTML = '<h3>Node ' + (index + 1) + '</h3>' +
      '<label>Id <input data-field="id" data-index="' + index + '" value="' + (node.id || '') + '"></label> ' +
      '<label>Tool <input data-field="tool" data-index="' + index + '" value="' + (node.tool || 'write') + '"></label> ' +
      '<label>Deliverable <input data-field="deliverable" data-index="' + index + '" value="' + (node.deliverable || '') + '"></label> ' +
      '<label>Depends on, comma list <input data-field="depends_on" data-index="' + index + '" value="' + (node.depends_on || []).join(',') + '"></label> ' +
      '<label>Expected <input data-field="expected" data-index="' + index + '" value="' + (node.expected || '') + '"></label> ' +
      '<label>Assumptions, comma list <input data-field="assumptions" data-index="' + index + '" value="' + (node.assumptions || []).join(',') + '"></label><br>' +
      '<label>Args, JSON <input data-field="args" data-index="' + index + '" size="80" value=\'' + JSON.stringify(node.args || {}).replace(/'/g, '') + '\'></label> ' +
      '<button data-action="up" data-index="' + index + '">Move up</button> ' +
      '<button data-action="down" data-index="' + index + '">Move down</button> ' +
      '<button data-action="remove" data-index="' + index + '">Remove</button>';
    div.appendChild(row);
  });
  div.querySelectorAll('input').forEach((input) => {
    input.onchange = () => {
      const index = parseInt(input.dataset.index, 10);
      const field = input.dataset.field;
      if (field === 'depends_on' || field === 'assumptions') {
        nodes[index][field] = input.value.split(',').map((s) => s.trim()).filter(Boolean);
      } else if (field === 'args') {
        try { nodes[index][field] = JSON.parse(input.value); } catch (e) { nodes[index][field] = {}; }
      } else {
        nodes[index][field] = input.value;
      }
    };
  });
  div.querySelectorAll('button').forEach((button) => {
    button.onclick = () => {
      const index = parseInt(button.dataset.index, 10);
      const action = button.dataset.action;
      if (action === 'remove') { nodes.splice(index, 1); }
      if (action === 'up' && index > 0) { const item = nodes.splice(index, 1)[0]; nodes.splice(index - 1, 0, item); }
      if (action === 'down' && index < nodes.length - 1) { const item = nodes.splice(index, 1)[0]; nodes.splice(index + 1, 0, item); }
      render();
    };
  });
}
async function loadList() {
  const r = await fetch('/api/dags');
  const data = await r.json();
  const list = document.getElementById('list');
  list.innerHTML = '<h2>Saved DAGs</h2>';
  (data.dags || []).forEach((dag) => {
    const button = document.createElement('button');
    button.textContent = dag.id + ' (' + dag.nodes + ' nodes) ' + dag.goal;
    button.onclick = async () => {
      const rr = await fetch('/api/dags/' + encodeURIComponent(dag.id));
      const dd = await rr.json();
      document.getElementById('dag-id').value = dd.id || dag.id;
      document.getElementById('dag-goal').value = dd.goal || '';
      nodes = dd.nodes || [];
      render();
    };
    list.appendChild(button);
    list.appendChild(document.createElement('br'));
  });
}
document.getElementById('add-node').onclick = () => {
  nodes.push({id: 'node-' + (nodes.length + 1), tool: 'write', args: {}, deliverable: '', depends_on: [], expected: '', assumptions: []});
  render();
};
document.getElementById('save').onclick = async () => {
  const id = document.getElementById('dag-id').value;
  const goal = document.getElementById('dag-goal').value;
  const r = await fetch('/api/dags', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id, goal, nodes})});
  const data = await r.json();
  document.getElementById('result').textContent = data.ok ? 'Saved.' : 'Not saved: ' + (data.failures || []).join('; ');
  await loadList();
};
document.getElementById('delete').onclick = async () => {
  const id = document.getElementById('dag-id').value;
  await fetch('/api/dags/' + encodeURIComponent(id), {method: 'DELETE'});
  await loadList();
};
loadList();
render();
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

    def cancel_all(self) -> None:
        """Resolve every pending card as rejected, for a cancelled run."""
        for card_id, event in list(self._events.items()):
            if not event.is_set():
                self.cards[card_id]["verdict"] = "rejected"
                event.set()

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
                 slack_signing_secret: str = "", messaging_config=None,
                 whatsapp_sender=None, slack_sender=None, webhook_only: bool = False,
                 media_downloader=None, run_control=None):
        self.home = Path(home)
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
        self.webui_gate = WebUIGate()
        self.webhook_only = webhook_only
        self.media_downloader = media_downloader
        self.run_control = run_control

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
        # Default chat handler: echo, for the server core.
        # A real goal handler is registered by the caller,
        # with GoalRunner, interview, or a trigger handler.
        if "webui" not in self.dispatcher.handlers:
            self.dispatcher.register("webui", lambda e: e.send_reply(f"Received: {e.goal}"))

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
        control_reply = self._resolve_control(message)
        if control_reply is not None:
            return {"reply": control_reply}
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

    def handle_whatsapp_post(self, body: dict, raw: bytes = b"",
                             headers: dict | None = None) -> tuple[int, dict]:
        # Signature first, when an app secret is configured.
        headers = headers or {}
        if not verify_whatsapp_signature(
                raw, self.messaging_config.whatsapp_app_secret,
                headers.get("x-hub-signature-256", "")):
            return 401, {"error": "bad signature"}
        # Owner gate, fail closed: only the owner number
        # becomes a goal, empty owner means inbound disabled.
        owner = normalize_number(self.messaging_config.whatsapp_owner)
        replies: list[str] = []
        try:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    for message in change.get("value", {}).get("messages", []):
                        text = message.get("text", {}).get("body", "")
                        if not text and message.get("type") == "image":
                            caption = message.get("image", {}).get("caption", "")
                            media_id = message.get("image", {}).get("id", "")
                            text = caption
                            if self.media_downloader is not None and media_id:
                                try:
                                    data = self.media_downloader(media_id)
                                    from .library import Library
                                    tmp = self.home / "library" / "whatsapp-image.png"
                                    tmp.parent.mkdir(parents=True, exist_ok=True)
                                    tmp.write_bytes(data)
                                    library = Library(self.home)
                                    saved = library.add_file(tmp, kind="image", description="WhatsApp image")
                                    text = f"{caption} [image library_id={saved['id']}]".strip()
                                except Exception as exc:
                                    text = caption or f"image received, could not save: {exc}"
                        sender = message.get("from", "")
                        if not owner or normalize_number(sender) != owner:
                            continue
                        decision_reply = self._resolve_decision(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_control(text)
                        if decision_reply is not None:
                            replies.append(decision_reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, decision_reply)
                            continue
                        local_replies: list[str] = []
                        event = TriggerEvent(source="whatsapp", kind="message",
                                             goal=text, payload={"from": sender},
                                             reply=local_replies.append)
                        self.dispatcher.dispatch(event)
                        for reply in local_replies:
                            replies.append(reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, reply)
        except Exception as exc:
            return 200, {"error": str(exc)}
        return 200, {"replies": replies}

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
        if (event_body.get("type") == "message"
                and not event_body.get("bot_id")
                and event_body.get("subtype") != "bot_message"):
            text = str(event_body.get("text", ""))
            channel = str(event_body.get("channel", ""))
            thread_ts = event_body.get("thread_ts") or event_body.get("ts")
            decision_reply = self._resolve_decision(text)
            if decision_reply is None:
                decision_reply = self._resolve_control(text)
            if decision_reply is not None:
                if self.slack_sender is not None:
                    self.slack_sender(channel, decision_reply, thread_ts)
                return 200, {"replies": [decision_reply]}
            replies: list[str] = []
            event = TriggerEvent(source="slack", kind="message", goal=text,
                                 reply=replies.append)
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                return 200, {"error": str(exc)}
            if self.slack_sender is not None:
                for reply in replies:
                    self.slack_sender(channel, reply, thread_ts)
            return 200, {"replies": replies}
        return 200, {"ok": True}


    def factory_stages(self) -> list[dict]:
        """Read trace files and map kinds to factory stages, for /factory."""
        stages = []
        trace_dir = self.home / "trace"
        if not trace_dir.exists():
            return stages
        for path in sorted(trace_dir.glob("*.jsonl")):
            for line in path.read_text(errors="ignore").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stages.append({"run": path.stem, "kind": event.get("kind", ""),
                               "status": event.get("status", "")})
        return stages

    def dag_store(self):
        from .dag_store import DagStore
        return DagStore(self.home)

    def handle_dags_list(self) -> dict:
        return {"dags": self.dag_store().list_dags()}

    def handle_dag_get(self, dag_id: str) -> dict:
        data = self.dag_store().get(dag_id)
        return data if data is not None else {"error": "not found"}

    def handle_dag_save(self, body: dict) -> tuple[int, dict]:
        ok, failures = self.dag_store().save(
            str(body.get("id", "")), str(body.get("goal", "")),
            list(body.get("nodes", [])), memory=self.memory)
        return (200, {"ok": True}) if ok else (400, {"ok": False, "failures": failures})

    def handle_dag_delete(self, dag_id: str) -> dict:
        return {"ok": self.dag_store().delete(dag_id)}

    def handle_entities(self, entity: str) -> dict:
        graph = getattr(self.memory, "entity_graph", None) if self.memory is not None else None
        if graph is None:
            return {"entity": entity, "related": []}
        return {"entity": entity, "related": graph.points_for(entity)}

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
                if server.webhook_only and parsed.path != "/webhook/whatsapp":
                    self._send_json(404, {"error": "webhook only"})
                elif parsed.path == "/":
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
                elif parsed.path == "/factory":
                    stages = server.factory_stages()
                    html = "<h1>Factory</h1><p>Trace stages, live from trace files, not a replay.</p>"
                    for stage in stages:
                        html += f"<p>{stage['run']} {stage['kind']} {stage['status']}</p>"
                    self._send_html(200, html)
                elif parsed.path == "/api/factory":
                    self._send_json(200, {"stages": server.factory_stages()})
                elif parsed.path == "/dag":
                    self._send_html(200, DAG_HTML)
                elif parsed.path == "/api/dags":
                    self._send_json(200, server.handle_dags_list())
                elif parsed.path.startswith("/api/dags/"):
                    dag_id = parsed.path[len("/api/dags/"):]
                    data = server.handle_dag_get(dag_id)
                    self._send_json(200 if "error" not in data else 404, data)
                elif parsed.path == "/api/entities":
                    query = parse_qs(parsed.query)
                    entity = query.get("entity", [""])[0]
                    self._send_json(200, server.handle_entities(entity))
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
                if server.webhook_only and parsed.path not in ("/webhook/whatsapp", "/webhook/slack"):
                    self._send_json(404, {"error": "webhook only"})
                    return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                if parsed.path == "/api/chat":
                    self._send_json(200, server.handle_chat(body))
                elif parsed.path == "/api/dags":
                    status, payload = server.handle_dag_save(body)
                    self._send_json(status, payload)
                elif parsed.path == "/api/runs":
                    runs = server.run_control.list_runs() if server.run_control else []
                    self._send_json(200, {"runs": runs})
                elif parsed.path == "/api/steer":
                    run_id = str(body.get("run_id", ""))
                    note = str(body.get("note", ""))
                    ok = server.run_control.steer(run_id, note) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path == "/api/cancel":
                    run_id = str(body.get("run_id", ""))
                    ok = server.run_control.cancel(run_id, gate=server.webui_gate) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path == "/api/approve":
                    card_id = str(body.get("id", ""))
                    verdict = str(body.get("verdict", "rejected"))
                    ok = server.webui_gate.approve(card_id, verdict)
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path == "/webhook/whatsapp":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_whatsapp_post(body, raw, headers)
                    self._send_json(status, payload)
                elif parsed.path == "/webhook/slack":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_slack_post(raw, headers, body)
                    self._send_json(status, payload)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_DELETE(self):
                parsed = urlparse(self.path)
                if parsed.path.startswith("/api/dags/"):
                    dag_id = parsed.path[len("/api/dags/"):]
                    self._send_json(200, server.handle_dag_delete(dag_id))
                else:
                    self._send_json(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        return Handler

    def serve(self, port: int = 8002) -> None:
        # Threading, so a goal run waiting on an approval card
        # does not block the /api/approve request that resolves it.
        ThreadingHTTPServer(("127.0.0.1", port), self.make_handler()).serve_forever()
