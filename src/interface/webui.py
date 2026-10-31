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
BASE_CSS = """
:root{--bg:#030b1c;--surface:#07152d;--surface2:#0c2040;--text:#e9eef8;--muted:#8b96b3;--border:#1e2a45;--accent:#0082fb;--accent2:#7c5cff;--green:#3ecf8e;--amber:#f5a623;--red:#f0564f}
body.light{--bg:#dfe8f4;--surface:#eef3f9;--surface2:#d5e1ef;--text:#131a2c;--muted:#5f6b87;--border:#c7d4e5;--accent:#0064e0;--accent2:#6a4dff;--green:#1f9d63;--amber:#c47b12;--red:#d43d36}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--text);margin:0 auto;padding:24px;max-width:980px;line-height:1.5}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:-1;background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:56px 56px;opacity:.14}
h1{font-family:Rajdhani,"Segoe UI",system-ui,sans-serif;font-size:1.8em;color:var(--text);margin:0 0 0.5em;text-transform:uppercase;letter-spacing:.12em}
h2{font-size:1.2em;color:var(--text);margin:1.2em 0 0.4em}
h3{font-size:1em;margin:1em 0 0.3em}
a{color:var(--accent)}
button{background:var(--accent);color:#fff;border:0;border-radius:6px;padding:7px 14px;font-size:1em;cursor:pointer}
button:hover{filter:brightness(1.15)}
input,select,textarea{border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:1em;background:var(--surface2);color:var(--text)}
label{margin-right:0.6em}
form{margin:0.8em 0}
pre{background:var(--surface2);border-radius:6px;padding:12px;overflow-x:auto;white-space:pre-wrap}
code{background:var(--surface2);border-radius:4px;padding:1px 5px}
table{border-collapse:collapse;margin:0.8em 0}
th,td{border:1px solid var(--border);padding:6px 10px;text-align:left}
th{background:var(--surface2)}
#chat{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-top:16px}
#messages{min-height:120px}
#messages p{background:var(--surface2);border-radius:10px;padding:10px 12px;margin:0.5em 0}
#messages table{background:var(--surface)}
#form{display:flex;gap:8px}
#msg{flex:1}
#pills span{display:inline-block;background:var(--surface2);border-radius:12px;padding:2px 10px;margin:2px;font-size:0.9em}
#avatar{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px;margin-bottom:16px}
#nodes>div{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:12px;margin:0.8em 0}
#factory{max-width:100%}
#vrm-view{max-width:100%}
#layout{display:flex;gap:20px;align-items:flex-start}
#sidecol{width:280px;flex-shrink:0}
#sidecol #avatar{width:auto;margin-bottom:20px}
#sidebar{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px}
#sidebar h2{font-size:1em;margin:1em 0 0.3em}
#sidebar h2:first-child{margin-top:0}
#sidebar a{display:block;margin:0.2em 0}
#chat{flex:1;margin-top:0;display:flex;flex-direction:column;min-height:60vh}
#messages{flex:1;overflow-y:auto}
#form{margin-top:12px}
#approval>div{background:rgba(245,166,35,.12);border:1px solid var(--amber);border-radius:10px;padding:12px;margin:0.8em 0}
#approval>div button{margin-right:8px}
body.chat-page{max-width:1200px}
body.chat-page h1{display:inline-block;margin-right:12px;vertical-align:middle}
#theme-toggle{padding:6px 12px;font-size:1.1em;vertical-align:middle}
.settings-shell{max-width:980px;margin:0 auto;padding:8px 0 40px}
.settings-top{display:flex;align-items:center;gap:14px;padding:8px 0 24px;border-bottom:1px solid var(--border)}
.settings-top h1{flex:1;margin:0;font-size:1.65em}
.settings-top a{font-size:.9em;text-decoration:none}
.settings-intro{padding:18px 0 10px;color:var(--muted);font-size:.92em}
.settings-intro code,.set-key{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.set-group{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:8px 20px 14px;margin:16px 0;box-shadow:0 12px 30px rgba(0,0,0,.12)}
.set-group h2{color:var(--accent);font-size:1em;letter-spacing:.16em;text-transform:uppercase;margin:12px 0 4px}
.set-row{display:flex;gap:22px;align-items:center;padding:14px 0;border-top:1px solid var(--border)}
.set-row:first-of-type{border-top:0}
.set-label{flex:1;min-width:0}.set-label label{font-weight:600}.set-key{display:block;font-size:.72em;color:var(--muted);margin-top:3px}.set-desc{font-size:.8em;color:var(--muted);line-height:1.4;margin-top:4px}
.set-badge{font-size:.68em;color:var(--green);border:1px solid var(--green);border-radius:5px;padding:1px 5px;margin-left:5px}
.set-input{flex:0 0 330px}.set-input input[type=text],.set-input input[type=password],.set-input input[type=number],.set-input select{width:100%;padding:9px 11px;border:1px solid var(--border);border-radius:9px;background:var(--surface2);color:var(--text);outline:0;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}.set-input input:focus,.set-input select:focus{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-glow)}
.range-control{display:grid;grid-template-columns:1fr 92px;gap:10px;align-items:center}.range-control input[type=range]{width:100%;accent-color:var(--accent)}.range-control input[type=number]{width:100%}.range-meta{display:flex;justify-content:space-between;margin-top:4px;color:var(--muted);font-size:.7em;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.switch{position:relative;display:inline-block;width:46px;height:25px}.switch input{opacity:0;width:0;height:0}.switch span{position:absolute;inset:0;border-radius:13px;background:var(--surface2);border:1px solid var(--border);cursor:pointer}.switch span:before{content:"";position:absolute;width:17px;height:17px;left:3px;top:3px;border-radius:50%;background:var(--muted);transition:.15s}.switch input:checked+span{background:var(--accent);border-color:var(--accent)}.switch input:checked+span:before{transform:translateX(21px);background:#fff}
#settings-footer{display:flex;align-items:center;gap:14px;padding:16px 0 28px}#settings-note{flex:1;color:var(--muted);font-size:.8em}#settings-save{padding:11px 24px;border:0;border-radius:10px;background:linear-gradient(92deg,var(--accent),var(--accent2));color:#fff;font-weight:700;letter-spacing:.12em;text-transform:uppercase;cursor:pointer}
@media(max-width:700px){.set-row{display:block}.set-input{width:100%;margin-top:10px}.settings-top{align-items:flex-start}.settings-shell{padding:0 4px 30px}}
@media (max-width:700px){#layout{display:block}#sidecol{width:auto;margin-bottom:20px}}
"""


def _page_html(title, body):
    """Wrap a handler-built fragment in a full styled page."""
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + title +
            "</title><link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head><body><script>try{if(localStorage.getItem('aidon-theme')==='light'){document.body.classList.add('light')}}catch(e){}</script>" + body + "</body></html>")


CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon</title>
<style>
#avatar { width: 220px; }
#robot { animation: robot-bob 4s ease-in-out infinite; transform-origin: 100px 190px; }
@keyframes robot-bob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
#robot-head { animation: robot-head-sway 6s ease-in-out infinite; transform-origin: 100px 60px; }
@keyframes robot-head-sway { 0%,100% { transform: rotate(0deg); } 25% { transform: rotate(1.5deg); } 75% { transform: rotate(-1.5deg); } }
#robot-arm-left { animation: robot-arm-sway 5s ease-in-out infinite; transform-origin: 35px 120px; }
#robot-arm-right { animation: robot-arm-sway 5s ease-in-out infinite reverse; transform-origin: 165px 120px; }
@keyframes robot-arm-sway { 0%,100% { transform: rotate(0deg); } 50% { transform: rotate(3deg); } }
.robot-eye { animation: robot-blink 4.2s infinite; transform-origin: center; transform-box: fill-box; }
@keyframes robot-blink { 0%,92%,98%,100% { transform: scaleY(1); } 94%,96% { transform: scaleY(0.1); } }
#mouth-smile { opacity: 1; }
#mouth-open { opacity: 0; }
#avatar.speaking #mouth-smile { opacity: 0; }
#avatar.speaking #mouth-open { opacity: 1; animation: robot-mouth-wave 0.28s ease-in-out infinite alternate; transform-origin: center; transform-box: fill-box; }
@keyframes robot-mouth-wave { from { transform: scaleY(0.4); } to { transform: scaleY(1.4); } }
#wave-bars span { display: inline-block; width: 6px; background: #0082fb; margin-right: 2px; vertical-align: bottom; height: 4px; }
#avatar.speaking #wave-bars span { animation: robot-wave 0.3s ease-in-out infinite alternate; }
@keyframes robot-wave { from { height: 4px; } to { height: 18px; } }
.karaoke-word { background: #ffeb3b; }
.karaoke-done { background: transparent; }
</style></head>
<body class="chat-page">
<h1>Aidon</h1><button id="theme-toggle" title="Toggle dark / light theme">Theme</button>
<div id="layout">
<div id="sidecol">
<div id="avatar" aria-label="Aidon robot avatar, default; VRM is available separately">
<svg id="robot-avatar" width="200" height="200" viewBox="0 0 200 200" role="img" aria-label="Aidon robot">
<g id="robot">
<g id="robot-head">
<rect x="28" y="35" width="16" height="42" rx="8" fill="#1a2a4a"/>
<rect x="156" y="35" width="16" height="42" rx="8" fill="#1a2a4a"/>
<rect x="40" y="18" width="120" height="72" rx="22" fill="#f5f5e6" stroke="#d9d9c2" stroke-width="2"/>
<rect x="55" y="28" width="90" height="52" rx="14" fill="#1a2a4a"/>
<rect x="62" y="34" width="76" height="40" rx="10" fill="#22355e"/>
<ellipse class="robot-eye" id="eye-left" cx="82" cy="54" rx="10" ry="16" fill="#7de9ff"/>
<ellipse class="robot-eye" id="eye-right" cx="118" cy="54" rx="10" ry="16" fill="#7de9ff"/>
<path id="mouth-smile" d="M 88 72 Q 100 78 112 72" fill="none" stroke="#7de9ff" stroke-width="3" stroke-linecap="round"/>
<ellipse id="mouth-open" cx="100" cy="73" rx="12" ry="6" fill="#7de9ff"/>
</g>
<rect x="92" y="90" width="16" height="12" fill="#c2c2b8"/>
<g id="robot-body">
<path d="M 62 102 Q 100 92 138 102 L 138 140 Q 100 155 62 140 Z" fill="#f5f5e6" stroke="#d9d9c2" stroke-width="2"/>
<path d="M 62 125 Q 100 140 138 125 L 138 140 Q 100 155 62 140 Z" fill="#c2c8d9"/>
</g>
<g id="robot-arm-left"><path d="M 58 108 Q 35 120 32 145 Q 32 160 45 160 L 58 150 Z" fill="#f5f5e6" stroke="#d9d9c2" stroke-width="2"/><circle cx="38" cy="162" r="4" fill="#1a2a4a"/><circle cx="48" cy="162" r="4" fill="#1a2a4a"/></g>
<g id="robot-arm-right"><path d="M 142 108 Q 165 120 168 145 Q 168 160 155 160 L 142 150 Z" fill="#f5f5e6" stroke="#d9d9c2" stroke-width="2"/><circle cx="152" cy="162" r="4" fill="#1a2a4a"/><circle cx="162" cy="162" r="4" fill="#1a2a4a"/></g>
<path d="M 75 155 Q 100 170 125 155 Q 100 165 75 155 Z" fill="#1a2a4a"/>
</svg>
<p id="avatar-status">Idle</p>
<div id="wave-bars"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
<p><button id="mic" type="button">Mic</button> <label><input id="announce" type="checkbox"> Voice announce</label></p>
<p id="mic-status"></p>
</div>
<div id="sidebar">
<h2>Library</h2><a href="/library">Library tab</a>
<h2>Settings</h2><a href="/settings">Settings and keys</a>
<h2>Workflows</h2><a href="/dag">DAG editor</a> <a href="/factory">Factory</a> <a href="/replay">Replay</a> <a href="/avatar-vrm">Open VRM avatar</a> <a href="/capabilities">Capabilities</a> <a href="/memory">Memory</a> <a href="/knowledge">Knowledge</a> <a href="/reports">Reports</a>
<h2>System status</h2><div id="system-status"></div>
<div id="stage-pills"><p>Stages: recall, interpret, research, plan, sandbox, verify, approval, live, learn</p><div id="pills"></div></div>
</div>
</div>
<div id="chat">
<div id="messages"></div>
<form id="form"><input id="msg" name="message" placeholder="Type a goal, in plain language, or use the mic">
<button type="submit">Send</button></form>
<div id="approval"></div>
</div>
</div>
<script>
function setAvatarStatus(status) {
  document.getElementById('avatar-status').textContent = status;
}
function setSpeaking(on) {
  document.getElementById('avatar').classList.toggle('speaking', !!on);
  const bars = document.querySelectorAll('#wave-bars span');
  bars.forEach((bar, i) => { bar.style.animationDelay = (i * 0.05) + 's'; });
}
function mouthForChar(ch) {
  if ('aeiouAEIOU'.includes(ch)) return 1.4;
  if (ch === ' ') return 0.4;
  return 0.9;
}
function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
function renderInline(s) {
  // s is already escaped: bold, code, and links are inserted as
  // markup AFTER escaping, so model text can never inject HTML.
  let out = s.replace(/`([^`]+)`/g, '<code>$1</code>');
  out = out.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  out = out.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^)\\s]+)\\)/g, '<a href="$2">$1</a>');
  return out;
}
function render_markdown(text) {
  // Hand-rolled, table-aware: consecutive lines starting with |
  // whose second line is a separator row of | - : and spaces
  // become a real <table>; other lines get inline rendering.
  // Non-table lines are joined with <br> so line breaks survive.
  const lines = text.split('\\n');
  let html = '';
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const next = lines[i + 1] || '';
    const isTable = line.trim().startsWith('|') && /^[\\s|:\\-]+$/.test(next) && next.includes('-');
    if (isTable) {
      const rows = [line];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith('|')) { rows.push(lines[i]); i++; }
      const cells = (row) => row.trim().replace(/^\\|/, '').replace(/\\|$/, '').split('|').map((c) => renderInline(escapeHtml(c.trim())));
      html += '<table><tr>' + cells(rows[0]).map((c) => '<th>' + c + '</th>').join('') + '</tr>';
      for (const row of rows.slice(1)) {
        html += '<tr>' + cells(row).map((c) => '<td>' + c + '</td>').join('') + '</tr>';
      }
      html += '</table>';
    } else {
      html += renderInline(escapeHtml(line));
      if (i < lines.length - 1) { html += '<br>'; }
      i++;
    }
  }
  return html;
}
async function typewriterKaraoke(element, text) {
  const words = text.split(/([ \t\n]+)/);
  let done = '';
  setSpeaking(true);
  setAvatarStatus('Speaking');
  for (let wi = 0; wi < words.length; wi++) {
    const word = words[wi];
    for (let ci = 0; ci < word.length; ci++) {
      const ch = word[ci];
      const mouth = document.getElementById('mouth-open');
      if (mouth) { mouth.setAttribute('ry', String(3 + mouthForChar(ch) * 4)); }
      done += ch;
      element.innerHTML = '<span class="karaoke-done">' + done.slice(0, done.length - word.length) + '</span>' +
        '<span class="karaoke-word">' + word.slice(0, ci + 1) + '</span>' +
        '<span>' + word.slice(ci + 1) + '</span>';
      await new Promise((r) => setTimeout(r, 18));
    }
  }
  // Completion hook: typing shows plain characters progressively,
  // but once typing finishes the message is re-rendered as
  // markdown, so tables, bold, code, and links display properly.
  element.innerHTML = render_markdown(text);
  setSpeaking(false);
  setAvatarStatus('Idle');
}
function speak(text, onWord) {
  if (document.getElementById('announce').checked && 'speechSynthesis' in window) {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.onstart = () => { setSpeaking(true); setAvatarStatus('Speaking'); };
    utterance.onend = () => { setSpeaking(false); setAvatarStatus('Idle'); };
    if (onWord) { utterance.onboundary = (event) => { if (event.charIndex !== undefined) { onWord(event.charIndex); } }; }
    window.speechSynthesis.speak(utterance);
  }
}
async function sendMessage(message) {
  setAvatarStatus('Working');
  const r = await fetch('/api/chat', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({message})});
  const data = await r.json();
  const reply = data.reply || '';
  const p = document.createElement('p');
  document.getElementById('messages').appendChild(p);
  await typewriterKaraoke(p, reply);
  const doneOpeners = ["Done:", "All done:", "Finished:", "That's done:", "Wrapped up:", "There we go:"];
  const failedOpeners = ["That run failed:", "It didn't get there:", "Something went wrong:", "That one didn't make it:"];
  if (doneOpeners.some(s => reply.includes(s))) { setAvatarStatus('Done'); }
  else if (failedOpeners.some(s => reply.includes(s))) { setAvatarStatus('Failed'); }
  else { setAvatarStatus('Idle'); }
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
async function refreshPills() {
  try {
    const r = await fetch('/api/factory');
    const data = await r.json();
    const latest = {};
    (data.stages || []).forEach((s) => { latest[s.kind] = s.status; });
    const kinds = ['recall','interpret','research','plan','sandbox','verify','approval','live','learn'];
    document.getElementById('pills').innerHTML = kinds.map((k) => '<span>' + k + '=' + (latest[k] || 'idle') + '</span> ').join('');
  } catch (e) {}
}
async function refreshSystemStatus() {
  try {
    const r = await fetch('/api/status');
    const data = await r.json();
    const box = document.getElementById('system-status');
    if (!box) { return; }
    const rows = [['Model', data.model], ['Backend', data.backend], ['Memory', data.memory], ['Search', data.web_search], ['Sandbox', data.sandbox], ['Voice', data.voice], ['Computer', data.computer], ['WhatsApp', data.whatsapp], ['Slack', data.slack]];
    let html = '';
    rows.forEach((row) => {
      let val = escapeHtml(String(row[1] || ''));
      if (row[0] === 'Memory' && data.memory_warning) {
        val = '<span style="background:#f5a623;color:#111;padding:2px 6px">' + val + '</span><br><small>' + escapeHtml(data.memory_warning) + '</small>';
      }
      html += '<p>' + escapeHtml(row[0]) + ': ' + val + '</p>';
    });
    box.innerHTML = html;
  } catch (e) {}
}
setInterval(refreshPills, 2000); refreshPills(); refreshSystemStatus(); setInterval(refreshSystemStatus, 30000);
async function refreshApprovals() {
  try {
    const r = await fetch('/api/approvals');
    const data = await r.json();
    const box = document.getElementById('approval');
    box.innerHTML = (data.cards || []).map((card) =>
      '<div><p><strong>' + escapeHtml(card.kind) + '</strong>: ' + escapeHtml(card.title) + '</p>' +
      '<p>' + escapeHtml(card.body) + '</p>' +
      '<button data-card="' + escapeHtml(card.id) + '" data-verdict="approved">Approve</button> ' +
      '<button data-card="' + escapeHtml(card.id) + '" data-verdict="rejected">Reject</button></div>'
    ).join('');
    box.querySelectorAll('button').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await fetch('/api/approve', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: btn.dataset.card, verdict: btn.dataset.verdict})});
        } catch (e) {}
        refreshApprovals();
      };
    });
  } catch (e) {}
}
setInterval(refreshApprovals, 2000); refreshApprovals();
try {
  if (localStorage.getItem('aidon-theme') === 'light') { document.body.classList.add('light'); }
  document.getElementById('theme-toggle').addEventListener('click', function() {
    var light = document.body.classList.toggle('light');
    try { localStorage.setItem('aidon-theme', light ? 'light' : 'dark'); } catch (e) {}
  });
} catch (e) {}
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

REPLAY_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Replay</title></head><body>
<h1>Run replay</h1><p>Replay a run from its trace file, step by step, not a live run. Pick a run, then Step to move through its trace events in order.</p>
<label>Run: <select id="run"></select></label> <button id="step">Step</button> <button id="play">Play</button>
<div id="events"></div>
<script>
let events = []; let index = 0;
async function loadRuns() {
  const r = await fetch('/api/replay');
  const data = await r.json();
  const sel = document.getElementById('run');
  sel.innerHTML = (data.runs || []).map((run) => '<option>' + run + '</option>').join('');
  if (data.runs && data.runs.length) { await loadRun(data.runs[0]); }
  sel.onchange = async () => { await loadRun(sel.value); };
}
async function loadRun(run) {
  const r = await fetch('/api/replay?run=' + encodeURIComponent(run));
  const data = await r.json();
  events = data.events || []; index = 0;
  document.getElementById('events').innerHTML = '';
}
document.getElementById('step').onclick = () => {
  if (index < events.length) {
    const e = events[index++];
    document.getElementById('events').innerHTML += '<p>' + (index) + '. ' + e.kind + ' ' + e.status + ' ' + (e.detail || '') + '</p>';
  }
};
document.getElementById('play').onclick = () => { const timer = setInterval(() => { if (index >= events.length) { clearInterval(timer); return; } document.getElementById('step').click(); }, 500); };
loadRuns();
</script></body></html>
"""
VRM_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>VRM avatar</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.min.js"></script>
</head><body>
<h1>VRM avatar (optional)</h1>
<p>The robot avatar is always the default in chat. This separate page is the explicit place to view a VRM file when one is present at &lt;home&gt;/avatar.vrm. Three.js and three-vrm are loaded from CDN, a VRM file is not bundled, because a model file is the owner's asset, not arena code. Mic here uses Meta Transcribe via /api/transcribe when a transcribe key is configured, otherwise browser SpeechRecognition, as in the chat.</p>
<div id="vrm-view" style="width:600px;height:400px;border:1px solid #ccc"></div>
<p id="vrm-status"></p>
<p><button id="vrm-mic">Transcribe mic</button> <span id="vrm-mic-status"></span></p>
<script>
async function checkVrm() {
  const r = await fetch('/api/avatar.vrm');
  if (r.status === 404) { document.getElementById('vrm-status').textContent = 'No VRM file found. The robot avatar remains the chat default.'; return; }
  document.getElementById('vrm-status').textContent = 'Optional VRM file found. The robot avatar remains the chat default.';
}
checkVrm();
document.getElementById('vrm-mic').onclick = async () => {
  document.getElementById('vrm-mic-status').textContent = 'Transcribe mic is browser SpeechRecognition in this build, Meta Transcribe is server-side at /api/transcribe when a key is set.';
};
</script></body></html>
"""

# VRM lip sync, wired in: the audio module's Web Audio script drives
# mouth morphs from audio RMS and blinks on a timer, not phonemes.
# It is appended to the VRM page, not a separate page, so the page
# above keeps its existing content and gains the sync script.
try:
    from ..capabilities.audio import VRM_LIP_SYNC_JS as _VRM_LIP_SYNC_JS
    VRM_HTML = VRM_HTML.replace(
        "</body></html>",
        "<script>\n" + _VRM_LIP_SYNC_JS +
        "\n// startVrmLipSync(vrm, audioElement): RMS mouth, blink timer.\n</script></body></html>")
except Exception:
    pass

FACTORY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon Factory</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.min.js"></script>
<style>body{font-family:sans-serif} #factory{width:900px;height:500px;border:1px solid #ccc} .legend{margin-top:8px}</style>
</head>
<body>
<h1>Factory</h1>
<p>Three-dimensional factory, live from trace files, not a replay and not a game. Each station is a run phase: receiving dock is intake, sorting is interpret, archive is research, blueprint table is plan, test bench is sandbox, inspection is verify, foreman desk is approval, assembly line is live, warehouse is learn. A station turns yellow while running, green when done, red when failed. The chat avatar stays in the chat, this factory is a separate view, linked from the chat sidebar.</p>
<div id="factory"></div>
<div class="legend" id="legend"></div>
<div id="runs"></div>
<script>
const stations = [
  {kind: 'interpret', label: 'Sorting', x: -8, z: 0},
  {kind: 'research', label: 'Archive', x: -4, z: 0},
  {kind: 'plan', label: 'Blueprint', x: 0, z: 0},
  {kind: 'sandbox', label: 'Test bench', x: 4, z: 0},
  {kind: 'verify', label: 'Inspection', x: 8, z: 0},
  {kind: 'approval', label: 'Foreman desk', x: 8, z: 4},
  {kind: 'live', label: 'Assembly line', x: 4, z: 4},
  {kind: 'learn', label: 'Warehouse', x: 0, z: 4},
  {kind: 'recall', label: 'Receiving dock', x: -8, z: 4}
];
const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf0f0f0);
const camera = new THREE.PerspectiveCamera(50, 900/500, 0.1, 100);
camera.position.set(0, 12, 14);
camera.lookAt(0, 0, 1);
const renderer = new THREE.WebGLRenderer({antialias: true});
renderer.setSize(900, 500);
document.getElementById('factory').appendChild(renderer.domElement);
const floor = new THREE.Mesh(new THREE.BoxGeometry(24, 0.2, 12), new THREE.MeshStandardMaterial({color: 0xcccccc}));
floor.position.set(0, -0.1, 2);
scene.add(floor);
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const light = new THREE.DirectionalLight(0xffffff, 0.8);
light.position.set(5, 10, 5);
scene.add(light);
function makeLabel(text) {
  const canvas = document.createElement('canvas');
  canvas.width = 256; canvas.height = 64;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 256, 64);
  ctx.fillStyle = '#000000'; ctx.font = '24px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText(text, 128, 40);
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({map: texture}));
  sprite.scale.set(4, 1, 1);
  return sprite;
}
const boxes = {};
stations.forEach((station) => {
  const box = new THREE.Mesh(new THREE.BoxGeometry(2.5, 1.5, 2.5), new THREE.MeshStandardMaterial({color: 0x999999}));
  box.position.set(station.x, 0.75, station.z);
  scene.add(box);
  boxes[station.kind] = box;
  const label = makeLabel(station.label);
  label.position.set(station.x, 2.2, station.z);
  scene.add(label);
});
function colourFor(status) {
  if (status === 'done') return 0x2eaa66;
  if (status === 'failed') return 0xcc3333;
  if (status === 'started') return 0xe6b800;
  return 0x999999;
}
async function refresh() {
  try {
    const r = await fetch('/api/factory');
    const data = await r.json();
    const latest = {};
    (data.stages || []).forEach((stage) => { latest[stage.kind] = stage.status; });
    stations.forEach((station) => {
      if (latest[station.kind]) { boxes[station.kind].material.color.setHex(colourFor(latest[station.kind])); }
    });
    document.getElementById('legend').textContent = 'Latest stages: ' + Object.entries(latest).map(([k, v]) => k + '=' + v).join(', ');
    const runs = {};
    (data.stages || []).forEach((stage) => { runs[stage.run] = (runs[stage.run] || 0) + 1; });
    document.getElementById('runs').textContent = 'Trace runs: ' + Object.entries(runs).map(([k, v]) => k + ' (' + v + ' stages)').join(', ');
  } catch (e) {}
  requestAnimationFrame(() => { scene.rotation.y += 0.001; });
}
setInterval(refresh, 2000);
refresh();
function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); }
animate();
</script>
</body></html>
"""


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
        self._counter = 0
        # Prefer shared AutonomyPolicy (same file as CLI)
        self.gate = ApprovalGate(self._decide, policy=policy)

    def _decide(self, kind: str, title: str, body: str,
                context: dict | None = None) -> Decision:
        import time as _time
        self._counter += 1
        card_id = f"card-{self._counter}"
        event = threading.Event()
        self._events[card_id] = event
        self.cards[card_id] = {
            "kind": kind, "title": title, "body": body, "verdict": None,
            "edited_text": "", "context": dict(context or {}),
        }
        pending_since = _time.time()
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
        return [{"id": card_id, "kind": card["kind"],
                 "title": card["title"], "body": card["body"]}
                for card_id, card in self.cards.items()
                if card["verdict"] is None]

    def cancel_all(self) -> None:
        """Resolve every pending card as rejected, for a cancelled run."""
        for card_id, event in list(self._events.items()):
            if not event.is_set():
                self.cards[card_id]["verdict"] = "rejected"
                event.set()

    def approve(self, card_id: str, verdict: str, edited_text: str = "") -> bool:
        if card_id not in self.cards:
            return False
        if verdict not in ("approved", "rejected", "edited"):
            return False
        self.cards[card_id]["verdict"] = verdict
        self.cards[card_id]["edited_text"] = str(edited_text or "")
        self._events[card_id].set()
        return True


HUB_MAX_EVENTS = 500


# Inject the shared style into the full-page constants defined above.
CHAT_HTML = CHAT_HTML.replace("</style></head>", "</style><style>" + BASE_CSS + "</style></head>", 1)
DAG_HTML = DAG_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
REPLAY_HTML = REPLAY_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
VRM_HTML = VRM_HTML.replace("</head>", "<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head>", 1)
FACTORY_HTML = FACTORY_HTML.replace("</head>", "<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head>", 1)


def _api_key_row(key: dict) -> str:
    """One API-key row of the settings page, escaped: key names are owner-supplied text."""
    name = html.escape(str(key.get("name", "")))
    scope = html.escape(str(key.get("scope", "")))
    prefix = html.escape(str(key.get("key_hash_prefix", "")))
    return (f"<div class=\"set-row\"><div class=\"set-label\"><label>{name}</label>"
            f"<span class=\"set-key\">{scope}</span></div><div class=\"set-input\">"
            f"<span class=\"set-key\">hash {prefix}</span></div></div>")


class WebUIServer:
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

    def handle_chat(self, body: dict) -> dict:
        message = str(body.get("message", ""))
        control_reply = self._resolve_control(message)
        if control_reply is not None:
            return {"reply": control_reply}
        preapproval_reply = self._resolve_preapproval(message)
        if preapproval_reply is not None:
            return {"reply": preapproval_reply}
        memory_reply = self._resolve_memory_command(message)
        if memory_reply is not None:
            return {"reply": memory_reply}
        message, force_web = self._force_web_prefix(message)
        if not message:
            return {"reply": "Usage: /web <question>"}
        message, delivery = extract_result_delivery(message)
        # A plain chat is global/unassigned unless a conversation id
        # is supplied with it.
        conversation_id = str(body.get("conversation_id", "") or "")
        replies: list[str] = []
        event = TriggerEvent(source="webui", kind="chat", goal=message,
                             conversation_id=conversation_id,
                             payload={**({"result_delivery": delivery.__dict__}
                                         if delivery else {}),
                                      "force_web": force_web},
                             reply=replies.append)
        try:
            self.dispatcher.dispatch(event)
        except Exception as exc:
            return {"reply": f"Error: {exc}"}
        return {"reply": replies[0] if replies else "Received."}

    def ask_question(self, run_id: str, conversation_id: str, question: str,
                     also_keys: tuple[str, ...] = ()) -> str:
        """Block the run thread until the owner answers in chat.

        The question is posted as a chat event so the owner sees it,
        and run_snapshot reports it as waiting_question so the
        composer stays usable. Questions carry no 900s clock (chat
        is for conversation, cards are for decisions); the wait ends
        on an answer or on run cancellation. also_keys registers
        the same waiter under linked surfaces, so an answer from
        Slack or WhatsApp resolves a WebUI run's question and
        vice versa (see surfaces.waiter_keys_*).
        """
        event = threading.Event()
        keys = [conversation_id] + [key for key in also_keys if key and key != conversation_id]
        waiter = {"run_id": run_id, "conversation_id": conversation_id,
                  "question": str(question), "event": event, "answer": None,
                  "keys": keys}
        with self._question_lock:
            self._question_waiters[run_id] = waiter
            for key in keys:
                self._question_by_conversation[key] = waiter
        record = self._web_runs.get(run_id)
        if record is not None:
            record["events"].append({"kind": "aidon", "text": str(question)})
        try:
            while not event.wait(1.0):
                if (self.run_control is not None
                        and self.run_control.is_cancelled(run_id)):
                    return ("QUESTION_CANCELLED: the run was cancelled "
                            "while waiting for an answer.")
            return waiter["answer"] if waiter["answer"] is not None else ""
        finally:
            with self._question_lock:
                if self._question_waiters.get(run_id) is waiter:
                    del self._question_waiters[run_id]
                for key in waiter.get("keys", [conversation_id]):
                    if self._question_by_conversation.get(key) is waiter:
                        del self._question_by_conversation[key]

    def answer_question(self, conversation_id: str, answer: str) -> str | None:
        """Deliver a chat answer to the waiting run in this conversation."""
        with self._question_lock:
            waiter = self._question_by_conversation.get(conversation_id)
            if waiter is None:
                return None
            waiter["answer"] = str(answer)
            waiter["event"].set()
            return waiter["run_id"]

    def waiting_question(self, run_id: str) -> str | None:
        with self._question_lock:
            waiter = self._question_waiters.get(run_id)
            return waiter["question"] if waiter else None

    def _mirror_command(self, conversation_id: str, goal: str) -> dict | None:
        """Handle a /mirror command in WebUI chat; None when not one."""
        from .surfaces import (link_slack, unlink_slack, set_whatsapp_mirror,
                               mirror_status_text, parse_mirror_command)
        command = parse_mirror_command(goal)
        if command is None:
            return None
        action = command["action"]
        if action == "link_slack":
            link_slack(self.home, conversation_id, command["channel"])
            text = f"Linked this conversation to Slack #{command['channel']}."
        elif action == "unlink_slack":
            removed = unlink_slack(self.home, conversation_id)
            text = ("Slack link removed." if removed
                    else "No Slack link to remove.")
        elif action == "whatsapp":
            set_whatsapp_mirror(self.home, conversation_id, command["enabled"])
            text = ("WhatsApp mirror on for this conversation."
                    if command["enabled"]
                    else "WhatsApp mirror off for this conversation.")
        elif action == "link_webui":
            text = ("You are already in this conversation; from Slack, "
                    "use /mirror webui <id> to link a channel here.")
        else:
            text = mirror_status_text(self.home, conversation_id=conversation_id)
        try:
            self._conversations_db().record_exchange(conversation_id, "assistant", text)
        except Exception:
            pass
        if self.memory is not None:
            try:
                self.memory.record_stm("assistant", text,
                                       conversation_id=conversation_id)
            except Exception:
                pass
        from .run_control import new_run_id
        run_id = new_run_id()
        self._web_runs[run_id] = {"run_id": run_id, "goal": goal,
                                  "conversation_id": conversation_id, "status": "done",
                                  "events": [{"kind": "aidon-final", "text": text,
                                              "stream_id": ""}], "trace": []}
        return {"run_id": run_id, "conversation_id": conversation_id, "goal": goal}

    def start_goal(self, body: dict) -> dict:
        """Start the same dispatcher path used by messaging triggers."""
        from .run_control import new_run_id
        goal = str(body.get("goal", "")).strip()
        if not goal:
            return {"error": "goal is required"}
        goal, force_web = self._force_web_prefix(goal)
        if not goal:
            return {"error": "a web question is required"}
        force_web = force_web or bool(body.get("force_web", False))
        goal, delivery = extract_result_delivery(goal)
        conversation_id = str(body.get("conversation_id", "") or "")
        conversation_db = None
        try:
            conversation_db = self._conversations_db()
            if not conversation_id:
                conversation_id = conversation_db.create_conversation()["id"]
            conversation_db.record_exchange(conversation_id, "user", goal)
        except Exception:
            # A run must still work if the optional conversation store is
            # unavailable; the durable run record remains authoritative.
            conversation_db = None
        if self.memory is not None:
            try:
                self.memory.record_stm(
                    "user", goal,
                    attachments=body.get("image_paths") or [],
                    conversation_id=conversation_id,
                )
            except Exception:
                pass
        # A message sent while this conversation's run waits for a
        # question IS the answer, not a new goal: the composer is
        # disabled while a run works and enabled only while it waits,
        # so a goal arriving here can only be an answer. Return the
        # same run id, as the frontend's answer path expects.
        answered_run = self.answer_question(conversation_id, goal)
        if answered_run is not None:
            return {"run_id": answered_run, "conversation_id": conversation_id,
                    "goal": goal}
        mirror_response = self._mirror_command(conversation_id, goal)
        if mirror_response is not None:
            return mirror_response
        from .mode_stops import latest_mode_stop, load_mode_stop
        from .surfaces import (HELP_TEXT, mode_enabled, parse_mode_command,
                               parse_proceed_command, parse_research_command,
                               parse_think_command, research_enabled,
                               set_mode, think_enabled, toggle_research,
                               toggle_think)
        if goal.strip() == "/help":
            return self._simple_command_response(conversation_id, goal,
                                                 HELP_TEXT)
        think_max = False
        think_cmd = parse_think_command(goal)
        if think_cmd is not None:
            if think_cmd["action"] == "toggle":
                state = toggle_think(self.home, conversation_id)
                return self._simple_command_response(
                    conversation_id, goal,
                    f"Maximum thinking {'on' if state else 'off'} for this conversation.")
            goal = think_cmd["goal"]
            think_max = True
        think_max = think_max or think_enabled(self.home, conversation_id)
        research_mode = False
        research_cmd = parse_research_command(goal)
        if research_cmd is not None:
            if research_cmd["action"] == "toggle":
                state = toggle_research(self.home, conversation_id)
                return self._simple_command_response(
                    conversation_id, goal,
                    f"Deep-research mode {'on' if state else 'off'} for this conversation.")
            goal = research_cmd["goal"]
            research_mode = True
        research_mode = research_mode or research_enabled(self.home, conversation_id)
        # Modes: a typed one-shot wins, then the composer's chooser
        # (payload "mode"), then the sticky flag for the conversation.
        # /proceed resumes a stopped run instead of starting a goal.
        mode = None
        resume_stop_id = ""
        resume_spec = None
        resume_plan = None
        mode_cmd = parse_mode_command(goal)
        if mode_cmd is not None:
            if mode_cmd["action"] == "set":
                set_mode(self.home, conversation_id, mode_cmd["mode"])
                if mode_cmd["mode"] == "execute":
                    _mtext = "Execute mode on for this conversation — goals run directly."
                else:
                    _mtext = f"{mode_cmd['mode'].capitalize()} mode on for this conversation."
                return self._simple_command_response(conversation_id, goal, _mtext)
            goal = mode_cmd["goal"]
            mode = mode_cmd["mode"]
        proceed_cmd = parse_proceed_command(goal)
        if proceed_cmd is not None:
            stop = (load_mode_stop(self.home, proceed_cmd["stop_id"])
                    if proceed_cmd["stop_id"]
                    else latest_mode_stop(self.home, conversation_id))
            if stop is None:
                if proceed_cmd["stop_id"]:
                    _ptext = f"No stopped run with id {proceed_cmd['stop_id']}."
                else:
                    _ptext = "No stopped run to proceed with."
                return self._simple_command_response(conversation_id, goal, _ptext)
            goal = stop["goal"]
            # Mode chain: Ask -> Plan -> Execute. Ask is for
            # beginners and detail, so its Proceed builds a plan
            # for approval; only the plan's Proceed executes. A
            # Plan stop proceeds straight to execution, as before.
            mode = "plan" if stop.get("mode") == "ask" else "execute"
            resume_stop_id = stop["stop_id"]
            resume_spec = stop.get("spec")
            resume_plan = stop.get("plan")
        if mode is None and str(body.get("mode", "") or "") in ("ask", "plan", "execute"):
            mode = str(body.get("mode"))
        if mode is None:
            mode = mode_enabled(self.home, conversation_id) or "ask"
        run_id = new_run_id()
        if self.run_control is not None:
            self.run_control.register(run_id, goal, conversation_id)
        record = {"run_id": run_id, "goal": goal, "conversation_id": conversation_id,
                  "status": "running", "events": [], "trace": []}
        self._web_runs[run_id] = record
        stream_state = {"text": "", "active": False}

        def stream_delta(delta):
            text = str(delta or "")
            if not text:
                return
            stream_state["active"] = True
            stream_state["text"] += text
            record["events"].append({"kind": "aidon-stream",
                                     "stream_id": run_id, "delta": text})

        def route_update(route):
            if not route or len(route) < 2:
                return
            record["route"] = {"group": str(route[0]),
                               "confidence": round(float(route[1]), 3)}

        def trace_update(item):
            if isinstance(item, dict):
                record["trace"].append(dict(item))

        def question_callback(question):
            # Fan out to linked surfaces now, and register the
            # waiter under their keys, so an answer from Slack or
            # WhatsApp resolves this WebUI run's question.
            from .surfaces import (slack_for_conversation,
                                   waiter_keys_for_conversation,
                                   whatsapp_mirrored_for_conversation)
            owner = ""
            try:
                owner = str(self.messaging_config.whatsapp_owner or "")
            except Exception:
                pass
            channel = slack_for_conversation(self.home, conversation_id)
            if channel and self.slack_sender is not None:
                try:
                    self.slack_sender(channel, str(question))
                except Exception:
                    pass
            if (owner and whatsapp_mirrored_for_conversation(self.home, conversation_id)
                    and self.whatsapp_sender is not None):
                try:
                    self.whatsapp_sender(owner, str(question))
                except Exception:
                    pass
            return self.ask_question(run_id, conversation_id, question,
                                     also_keys=tuple(waiter_keys_for_conversation(
                                         self.home, conversation_id, owner)))

        def reply(text):
            record["events"].append({"kind": "aidon-final", "text": str(text),
                                     "stream_id": run_id if stream_state["active"] else ""})
            if conversation_db is not None:
                try:
                    conversation_db.record_exchange(conversation_id, "assistant", str(text))
                except Exception:
                    pass
            if self.memory is not None:
                try:
                    self.memory.record_stm(
                        "assistant", str(text),
                        conversation_id=conversation_id)
                except Exception:
                    pass

        payload = {"run_id": run_id,
                   "guidance": str(body.get("guidance", "")),
                   "force_web": force_web,
                   "think_max": think_max,
                   "research_mode": research_mode,
                   "_stream_callback": stream_delta,
                   "_route_callback": route_update,
                   "_trace_callback": trace_update,
                   "_question_callback": question_callback,
                   "result_delivery": delivery.__dict__ if delivery else {}}
        image_paths = [str(path) for path in (body.get("image_paths") or [])
                       if str(path).strip()]
        if image_paths:
            payload["image_paths"] = image_paths
        payload["mode"] = mode
        if resume_stop_id:
            payload["resume_stop_id"] = resume_stop_id
        if isinstance(resume_spec, dict):
            payload["resume_spec"] = resume_spec
        if isinstance(resume_plan, dict):
            payload["resume_plan"] = resume_plan
        event = TriggerEvent(source="webui", kind="goal", goal=goal,
                             conversation_id=conversation_id,
                             payload={**payload, "conversation_id": conversation_id},
                             reply=reply)

        def dispatch():
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                record["events"].append({"kind": "aidon-final",
                                         "text": f"Error: {exc}"})
                if self.run_control is not None:
                    self.run_control.set_status(run_id, "failed")

        threading.Thread(target=dispatch, daemon=True,
                         name=f"aidon-run-{run_id}").start()
        return {"run_id": run_id, "conversation_id": conversation_id,
                "goal": goal}

    def run_snapshot(self, run_id: str, since: int = 0, trace_since: int = 0) -> dict | None:
        record = self._web_runs.get(run_id)
        control = self.run_control.get(run_id) if self.run_control is not None else None
        if record is None and control is None:
            return None
        out = dict(record or {})
        if control:
            terminal = control.get("status") in {
                "done", "failed", "rejected", "cancelled", "stopped"}
            out.update({"status": control.get("status", out.get("status", "running")),
                        "goal": control.get("goal", out.get("goal", "")),
                        "stage": ("done" if terminal else
                                   control.get("stage", out.get("stage", "interpret")))})
        out.setdefault("events", [])
        out.setdefault("trace", [])
        # The browser polls incrementally. Returning the complete history on
        # every poll caused each old assistant event to be rendered again,
        # which looked like an infinite model loop.
        all_events = out["events"]
        all_trace = out["trace"]
        out["event_total"] = len(all_events)
        out["trace_total"] = len(all_trace)
        out["events"] = all_events[max(0, int(since)):]
        out["trace"] = all_trace[max(0, int(trace_since)):]
        out["waiting_question"] = self.waiting_question(run_id)
        pending = self.webui_gate.pending_cards()
        out["pending"] = pending[0] if pending else None
        return out

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
                                    from ..memory.library import Library
                                    tmp = self.home / "library" / "whatsapp-image.png"
                                    tmp.parent.mkdir(parents=True, exist_ok=True)
                                    tmp.write_bytes(data)
                                    library = Library(self.home)
                                    saved = library.add_file(tmp, kind="image", description="WhatsApp image")
                                    try:
                                        from ..capabilities.whatsapp_vision import prepare_vision_message
                                        vision = prepare_vision_message(str(tmp), caption)
                                        text = f"{caption} [image library_id={saved['id']} vision_image={vision['image_path']}]".strip()
                                    except Exception:
                                        text = f"{caption} [image library_id={saved['id']}]".strip()
                                except Exception as exc:
                                    text = caption or f"image received, could not save: {exc}"
                        elif not text and message.get("type") in ("document", "video", "audio"):
                            # Same download path as images (the Graph
                            # media-download API, two steps), no vision
                            # step: the file lands in the library and
                            # the goal text carries its library id.
                            mtype = message["type"]
                            media = message.get(mtype, {}) or {}
                            media_id = media.get("id", "")
                            caption = media.get("caption", "")
                            filename = media.get("filename", "") or f"whatsapp-{mtype}"
                            text = caption
                            if self.media_downloader is not None and media_id:
                                try:
                                    data = self.media_downloader(media_id)
                                    from ..memory.library import Library
                                    safe = self._safe_name(filename)
                                    tmp = self.home / "library" / f"whatsapp-{media_id}-{safe}"
                                    tmp.parent.mkdir(parents=True, exist_ok=True)
                                    tmp.write_bytes(data)
                                    library = Library(self.home)
                                    saved = library.add_file(tmp, kind="attachment",
                                                             description=f"WhatsApp {mtype}")
                                    text = (f"{caption} [file library_id={saved['id']} "
                                            f"kind={mtype}]").strip()
                                except Exception as exc:
                                    text = caption or f"{mtype} received, could not save: {exc}"
                        sender = message.get("from", "")
                        if not owner or normalize_number(sender) != owner:
                            continue
                        decision_reply = self._resolve_decision(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_control(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_preapproval(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_memory_command(text)
                        # An answer to a waiting question is not a new
                        # goal: deliver it to the waiter, ack in natural
                        # language, and skip dispatching a new run.
                        if decision_reply is None:
                            answered = self.answer_question(
                                "whatsapp:" + str(sender), text)
                            if answered is not None:
                                from .phrasing import question_ack as _qack
                                ack = _qack()
                                replies.append(ack)
                                if self.whatsapp_sender is not None:
                                    self.whatsapp_sender(sender, ack)
                                continue
                        from .surfaces import (HELP_TEXT as _WA_HELP,
                                                mode_enabled as _wa_mode_enabled,
                                                parse_mode_command as _wa_mode_cmd,
                                                parse_proceed_command as _wa_proceed,
                                                parse_research_command as _wa_research,
                                                parse_think_command as _wa_think,
                                                research_enabled as _wa_research_enabled,
                                                set_mode as _wa_set_mode,
                                                think_enabled as _wa_enabled,
                                                toggle_research as _wa_research_toggle,
                                                toggle_think as _wa_toggle)
                        from .mode_stops import (latest_mode_stop as _wa_latest_stop,
                                                 load_mode_stop as _wa_load_stop)
                        wa_key = "whatsapp:" + str(sender)
                        wa_think_max = False
                        wa_research_mode = False
                        wa_mode = None
                        wa_resume_stop_id = ""
                        wa_resume_spec = None
                        wa_resume_plan = None
                        if decision_reply is None:
                            wa_cmd = _wa_think(text)
                            if wa_cmd is not None:
                                if wa_cmd["action"] == "toggle":
                                    state = _wa_toggle(self.home, wa_key)
                                    decision_reply = (
                                        f"Maximum thinking {'on' if state else 'off'} "
                                        "for this chat.")
                                else:
                                    text = wa_cmd["goal"]
                                    wa_think_max = True
                            else:
                                wa_rcmd = _wa_research(text)
                                if wa_rcmd is not None:
                                    if wa_rcmd["action"] == "toggle":
                                        state = _wa_research_toggle(self.home, wa_key)
                                        decision_reply = (
                                            f"Deep-research mode {'on' if state else 'off'} "
                                            "for this chat.")
                                    else:
                                        text = wa_rcmd["goal"]
                                        wa_research_mode = True
                                elif text.strip() == "/help":
                                    decision_reply = _WA_HELP
                            if decision_reply is None:
                                wa_mcmd = _wa_mode_cmd(text)
                                if wa_mcmd is not None:
                                    if wa_mcmd["action"] == "set":
                                        _wa_set_mode(self.home, wa_key, wa_mcmd["mode"])
                                        if wa_mcmd["mode"] == "execute":
                                            decision_reply = "Execute mode on for this chat — goals run directly."
                                        else:
                                            decision_reply = (f"{wa_mcmd['mode'].capitalize()} mode on "
                                                              "for this chat.")
                                    else:
                                        text = wa_mcmd["goal"]
                                        wa_mode = wa_mcmd["mode"]
                                else:
                                    wa_pcmd = _wa_proceed(text)
                                    if wa_pcmd is not None:
                                        _stop = (_wa_load_stop(self.home, wa_pcmd["stop_id"])
                                                 if wa_pcmd["stop_id"]
                                                 else _wa_latest_stop(self.home, wa_key))
                                        if _stop is None:
                                            if wa_pcmd["stop_id"]:
                                                decision_reply = f"No stopped run with id {wa_pcmd['stop_id']}."
                                            else:
                                                decision_reply = "No stopped run to proceed with."
                                        else:
                                            text = _stop["goal"]
                                            wa_mode = "execute"
                                            wa_resume_stop_id = _stop["stop_id"]
                                            wa_resume_spec = _stop.get("spec")
                                            wa_resume_plan = _stop.get("plan")
                        if decision_reply is None:
                            text, force_web = self._force_web_prefix(text)
                            if not text:
                                decision_reply = "Usage: /web <question>"
                            else:
                                decision_reply = None
                        if decision_reply is not None:
                            replies.append(decision_reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, decision_reply)
                            continue
                        local_replies: list[str] = []
                        _wa_payload = {"from": sender,
                                       "force_web": force_web,
                                       "think_max": wa_think_max or _wa_enabled(self.home, wa_key),
                                       "research_mode": wa_research_mode or _wa_research_enabled(self.home, wa_key),
                                       "mode": wa_mode or _wa_mode_enabled(self.home, wa_key) or "ask"}
                        if wa_resume_stop_id:
                            _wa_payload["resume_stop_id"] = wa_resume_stop_id
                        if isinstance(wa_resume_spec, dict):
                            _wa_payload["resume_spec"] = wa_resume_spec
                        if isinstance(wa_resume_plan, dict):
                            _wa_payload["resume_plan"] = wa_resume_plan
                        event = TriggerEvent(source="whatsapp", kind="message",
                                             goal=text, payload=_wa_payload,
                                             reply=local_replies.append)
                        self.dispatcher.dispatch(event)
                        for reply in local_replies:
                            replies.append(reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, reply)
        except Exception as exc:
            return 200, {"error": str(exc)}
        return 200, {"replies": replies}

    def _record_surface_exchange(self, conversation_id: str, role: str, text: str) -> None:
        """Best-effort conversation-store record for a non-WebUI surface."""
        try:
            self._conversations_db().record_exchange(conversation_id, role, text)
        except Exception:
            pass

    def _simple_command_response(self, conversation_id: str, goal: str,
                               text: str) -> dict:
        """Reply to a chat command without running a goal.

        Same shape as a mirror-command reply: the assistant text is
        recorded in the conversation and a done run carries it to the
        frontend, so /think and /help behave like any other answer.
        """
        try:
            self._conversations_db().record_exchange(conversation_id, "assistant", text)
        except Exception:
            pass
        if self.memory is not None:
            try:
                self.memory.record_stm("assistant", text,
                                       conversation_id=conversation_id)
            except Exception:
                pass
        from .run_control import new_run_id
        run_id = new_run_id()
        self._web_runs[run_id] = {"run_id": run_id, "goal": goal,
                                  "conversation_id": conversation_id, "status": "done",
                                  "events": [{"kind": "aidon-final", "text": text,
                                              "stream_id": ""}], "trace": []}
        return {"run_id": run_id, "conversation_id": conversation_id, "goal": goal}

    def _slack_mirror_command(self, channel: str, text: str) -> str | None:
        """Handle a /mirror command from Slack; None when not one."""
        from .surfaces import (conversation_for_slack, link_slack, unlink_slack,
                               mirror_status_text, parse_mirror_command,
                               set_whatsapp_mirror)
        command = parse_mirror_command(text)
        if command is None:
            return None
        action = command["action"]
        if action == "link_webui":
            link_slack(self.home, command["conversation"], channel)
            return (f"Linked this channel to WebUI conversation "
                    f"{command['conversation']}.")
        if action == "unlink_slack":
            conversation = conversation_for_slack(self.home, channel)
            if conversation and unlink_slack(self.home, conversation):
                return "Slack link removed."
            return "No Slack link to remove."
        if action == "whatsapp":
            conversation = (conversation_for_slack(self.home, channel)
                            or ("slack:" + channel))
            set_whatsapp_mirror(self.home, conversation, command["enabled"])
            return ("WhatsApp mirror on for this channel."
                    if command["enabled"]
                    else "WhatsApp mirror off for this channel.")
        if action == "link_slack":
            return "From Slack, link with /mirror webui <conversation-id>."
        return mirror_status_text(self.home, channel=channel)

    def handle_slack_post(self, raw: bytes, headers: dict, body: dict) -> tuple[int, dict]:
        # Verify signature before parsing any event as a goal.
        # Fail closed: without a configured signing secret no
        # Slack request is verified, so none is dispatched.
        if not self.slack_signing_secret:
            return 401, {"error": "slack signing secret not configured"}
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
            if decision_reply is None:
                decision_reply = self._resolve_preapproval(text)
            if decision_reply is None:
                decision_reply = self._resolve_memory_command(text)
            mirror_reply = self._slack_mirror_command(channel, text)
            if mirror_reply is not None:
                if self.slack_sender is not None:
                    self.slack_sender(channel, mirror_reply, thread_ts)
                self._record_surface_exchange("slack:" + channel, "user", text)
                self._record_surface_exchange("slack:" + channel, "assistant", mirror_reply)
                return 200, {"replies": [mirror_reply]}
            from .surfaces import (HELP_TEXT as _SL_HELP,
                                    mode_enabled as _sl_mode_enabled,
                                    parse_mode_command as _sl_mode_cmd,
                                    parse_proceed_command as _sl_proceed,
                                    parse_research_command as _sl_research,
                                    parse_think_command as _sl_think,
                                    research_enabled as _sl_research_enabled,
                                    set_mode as _sl_set_mode,
                                    think_enabled as _sl_enabled,
                                    toggle_research as _sl_research_toggle,
                                    toggle_think as _sl_toggle)
            from .mode_stops import (latest_mode_stop as _sl_latest_stop,
                                     load_mode_stop as _sl_load_stop)
            sl_key = "slack:" + channel
            sl_think_max = False
            sl_research_mode = False
            sl_mode = None
            sl_resume_stop_id = ""
            sl_resume_spec = None
            sl_resume_plan = None
            if decision_reply is None:
                sl_cmd = _sl_think(text)
                if sl_cmd is not None:
                    if sl_cmd["action"] == "toggle":
                        state = _sl_toggle(self.home, sl_key)
                        decision_reply = (f"Maximum thinking {'on' if state else 'off'} "
                                          "for this channel.")
                    else:
                        text = sl_cmd["goal"]
                        sl_think_max = True
                else:
                    sl_rcmd = _sl_research(text)
                    if sl_rcmd is not None:
                        if sl_rcmd["action"] == "toggle":
                            state = _sl_research_toggle(self.home, sl_key)
                            decision_reply = (f"Deep-research mode {'on' if state else 'off'} "
                                              "for this channel.")
                        else:
                            text = sl_rcmd["goal"]
                            sl_research_mode = True
                    elif text.strip() == "/help":
                        decision_reply = _SL_HELP
                if decision_reply is None:
                    sl_mcmd = _sl_mode_cmd(text)
                    if sl_mcmd is not None:
                        if sl_mcmd["action"] == "set":
                            _sl_set_mode(self.home, sl_key, sl_mcmd["mode"])
                            if sl_mcmd["mode"] == "execute":
                                decision_reply = "Execute mode on for this channel — goals run directly."
                            else:
                                decision_reply = (f"{sl_mcmd['mode'].capitalize()} mode on "
                                                  "for this channel.")
                        else:
                            text = sl_mcmd["goal"]
                            sl_mode = sl_mcmd["mode"]
                    else:
                        sl_pcmd = _sl_proceed(text)
                        if sl_pcmd is not None:
                            _stop = (_sl_load_stop(self.home, sl_pcmd["stop_id"])
                                     if sl_pcmd["stop_id"]
                                     else _sl_latest_stop(self.home, sl_key))
                            if _stop is None:
                                if sl_pcmd["stop_id"]:
                                    decision_reply = f"No stopped run with id {sl_pcmd['stop_id']}."
                                else:
                                    decision_reply = "No stopped run to proceed with."
                            else:
                                text = _stop["goal"]
                                sl_mode = "execute"
                                sl_resume_stop_id = _stop["stop_id"]
                                sl_resume_spec = _stop.get("spec")
                                sl_resume_plan = _stop.get("plan")
            force_web = False
            if decision_reply is None:
                text, force_web = self._force_web_prefix(text)
                if not text:
                    decision_reply = "Usage: /web <question>"
            if decision_reply is None:
                # An answer to a waiting question is not a new goal.
                answered = self.answer_question("slack:" + channel, text)
                if answered is not None:
                    from .phrasing import question_ack as _qack
                    ack = _qack()
                    if self.slack_sender is not None:
                        self.slack_sender(channel, ack, thread_ts)
                    self._record_surface_exchange("slack:" + channel, "user", text)
                    self._record_surface_exchange("slack:" + channel, "assistant", ack)
                    return 200, {"replies": [ack]}
            if decision_reply is not None:
                if self.slack_sender is not None:
                    self.slack_sender(channel, decision_reply, thread_ts)
                return 200, {"replies": [decision_reply]}
            caption = text
            replies: list[str] = []
            # Image intake, same shape as parse_slack_event: image
            # files ride with the goal text as [image ...] markers,
            # other files as [file ...] markers. Decision replies
            # above already ran on the raw text, so replacing the
            # goal text here cannot change an approval verdict.
            # When the server's messaging config carries a Slack
            # bot token, the file bytes are downloaded here
            # (mirroring the WhatsApp path: library save + vision
            # message for images, library save for documents);
            # without a token, markers + metadata only.
            payload: dict = {}
            try:
                from .slack_inbound import parse_slack_event as _parse_slack
                _parsed = _parse_slack(body)
                if _parsed is not None and (_parsed.payload.get("images")
                                           or _parsed.payload.get("files")):
                    text = _parsed.goal
                    payload = _parsed.payload
            except Exception:
                pass
            if payload.get("images") and self.messaging_config.slack_bot_token:
                from .messaging import download_slack_file as _dl_slack
                for image in payload["images"]:
                    url = image.get("url_private", "")
                    if not url:
                        continue
                    try:
                        data = _dl_slack(self.messaging_config.slack_bot_token, url)
                    except Exception:
                        continue
                    try:
                        from ..memory.library import Library
                        ext = {"image/png": "png", "image/jpeg": "jpg",
                               "image/gif": "gif", "image/webp": "webp"}.get(
                            image.get("mimetype", ""), "png")
                        tmp = self.home / "library" / f"slack-{image['id']}.{ext}"
                        tmp.parent.mkdir(parents=True, exist_ok=True)
                        tmp.write_bytes(data)
                        library = Library(self.home)
                        saved = library.add_file(tmp, kind="image", description="Slack image")
                        marker = (f"[image slack_file_id={image['id']} "
                                  f"mimetype={image['mimetype']} name={image['name']}]")
                        try:
                            from ..capabilities.whatsapp_vision import prepare_vision_message
                            vision = prepare_vision_message(str(tmp), caption)
                            replacement = (f"[image library_id={saved['id']} "
                                           f"vision_image={vision['image_path']}]")
                        except Exception:
                            replacement = f"[image library_id={saved['id']}]"
                        text = text.replace(marker, replacement)
                        image["downloaded"] = True
                    except Exception:
                        continue
            # Non-image files: same download, no vision step — the
            # document lands in the library and the marker carries
            # its library id, so the run can read it.
            if payload.get("files") and self.messaging_config.slack_bot_token:
                from .messaging import download_slack_file as _dl_slack_file
                for shared in payload["files"]:
                    url = shared.get("url_private", "")
                    if not url:
                        continue
                    try:
                        data = _dl_slack_file(self.messaging_config.slack_bot_token, url)
                    except Exception:
                        continue
                    try:
                        from ..memory.library import Library
                        safe = self._safe_name(str(shared.get("name", "slack-file")))
                        tmp = self.home / "library" / f"slack-{shared['id']}-{safe}"
                        tmp.parent.mkdir(parents=True, exist_ok=True)
                        tmp.write_bytes(data)
                        library = Library(self.home)
                        saved = library.add_file(tmp, kind="attachment",
                                                 description="Slack file")
                        marker = (f"[file slack_file_id={shared['id']} "
                                  f"mimetype={shared['mimetype']} name={shared['name']}]")
                        text = text.replace(
                            marker, f"[file library_id={saved['id']}]")
                        shared["downloaded"] = True
                    except Exception:
                        continue
            payload["force_web"] = force_web
            payload["think_max"] = sl_think_max or _sl_enabled(self.home, sl_key)
            payload["research_mode"] = sl_research_mode or _sl_research_enabled(self.home, sl_key)
            payload["mode"] = sl_mode or _sl_mode_enabled(self.home, sl_key) or "ask"
            if sl_resume_stop_id:
                payload["resume_stop_id"] = sl_resume_stop_id
            if isinstance(sl_resume_spec, dict):
                payload["resume_spec"] = sl_resume_spec
            if isinstance(sl_resume_plan, dict):
                payload["resume_plan"] = sl_resume_plan
            payload["channel"] = channel
            payload["thread_ts"] = thread_ts
            event = TriggerEvent(source="slack", kind="message", goal=text,
                                 payload=payload,
                                 reply=replies.append)
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                return 200, {"error": str(exc)}
            if self.slack_sender is not None:
                for reply in replies:
                    self.slack_sender(channel, reply, thread_ts)
            self._record_surface_exchange("slack:" + channel, "user", text)
            for reply in replies:
                self._record_surface_exchange("slack:" + channel, "assistant", reply)
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
        from ..agentic.dag_store import DagStore
        return DagStore(self.home)

    def handle_dags_list(self) -> dict:
        return {"dags": self.dag_store().list_dags()}

    def handle_dag_get(self, dag_id: str) -> dict:
        data = self.dag_store().get(dag_id)
        return data if data is not None else {"error": "not found"}

    def handle_dag_save(self, body: dict) -> tuple[int, dict]:
        ok, failures = self.dag_store().save(
            str(body.get("id", "")), str(body.get("goal", "")),
            list(body.get("nodes", [])), memory=self.memory, merge=bool(body.get("merge", False)))
        # Memory DAGs are also editable: if the editor asks for a
        # memory DAG, it is loaded via /api/memory-dags below, and
        # saved back through this same save, with merge, so the
        # editor no longer edits only the file store.
        return (200, {"ok": True}) if ok else (400, {"ok": False, "failures": failures})

    def handle_dag_delete(self, dag_id: str) -> dict:
        return {"ok": self.dag_store().delete(dag_id)}

    def replay_runs(self) -> list[str]:
        trace_dir = self.home / "trace"
        if not trace_dir.exists():
            return []
        return sorted(path.stem for path in trace_dir.glob("*.jsonl"))

    def replay_events(self, run: str) -> list[dict]:
        path = self.home / "trace" / f"{run}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(errors="ignore").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def settings_get(self) -> dict:
        path = self.home / "settings.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def settings_set(self, key: str, value) -> bool:
        allowed = {"timezone", "budget_tokens", "max_steps", "autonomy_enabled", "approval_nudge_minutes",
                   "research_max_rounds", "max_stuck_iterations", "parallel_tools"}
        if key not in allowed:
            return False
        data = self.settings_get()
        data[key] = value
        path = self.home / "settings.json"
        path.write_text(json.dumps(data, indent=2))
        return True

    # -- WebUI depth: settings into .env, studio edits, plugin adoption.
    #    These are new methods only; the existing /api/settings
    #    behaviour above (settings.json, 8 keys) is unchanged. ------
    _ENV_SETTING_MAP = {
        "timezone": "AIDON_TIMEZONE",
        "budget_tokens": "AIDON_MAX_TOKENS_PER_RUN",
        "max_steps": "AIDON_MAX_STEPS",
        "autonomy_enabled": "AIDON_AUTONOMY_ENABLED",
        "approval_nudge_minutes": "AIDON_APPROVAL_NUDGE_MIN",
        "research_max_rounds": "AIDON_RESEARCH_MAX_ROUNDS",
        "max_stuck_iterations": "AIDON_MAX_STUCK_ITERATIONS",
        "parallel_tools": "AIDON_PARALLEL_TOOLS",
        "daily_report_time": "AIDON_DAILY_REPORT_TIME",
        "execution_backend": "AIDON_EXECUTION_BACKEND",
        "search_backend": "AIDON_SEARCH_BACKEND",
        "searxng_url": "AIDON_SEARXNG_URL",
        "qdrant_url": "AIDON_QDRANT_URL",
        "image_model": "AIDON_IMAGE_MODEL",
        "sam_model": "AIDON_SAM_MODEL",
        "owner_email": "AIDON_OWNER_EMAIL",
        "model": "AIDON_MODEL",
        "fast_model": "AIDON_FAST_MODEL",
        "verifier_model": "AIDON_VERIFIER_MODEL",
        "voice_announce": "AIDON_VOICE_ANNOUNCE",
        "sam_base_url": "AIDON_SAM_BASE_URL",
        "sam_api_key": "AIDON_SAM_API_KEY",
        "tts_voice_id": "AIDON_TTS_VOICE_ID",
        "backend": "AIDON_BACKEND",
        "api_endpoint": "AIDON_API_ENDPOINT",
        "embed_model": "AIDON_EMBED_MODEL",
        "sparse_model": "AIDON_SPARSE_MODEL",
        "sandbox_timeout_seconds": "AIDON_MAX_SANDBOX_SECONDS",
        "approval_timeout_seconds": "AIDON_APPROVAL_TIMEOUT_S",
        "docker_image": "AIDON_DOCKER_IMAGE",
        "docker_network": "AIDON_DOCKER_NETWORK",
        "ingest_dir": "AIDON_INGEST_DIR",
        "wallclock_minutes": "AIDON_MAX_WALLCLOCK_MIN",
        "model_timeout_seconds": "AIDON_MODEL_TIMEOUT_S",
        "forget_half_life_days": "AIDON_FORGET_HALF_LIFE_DAYS",
        "forget_grace_days": "AIDON_FORGET_GRACE_DAYS",
        "forget_cleanup_threshold": "AIDON_FORGET_CLEANUP_THRESHOLD",
        "whatsapp_phone_id": "AIDON_WHATSAPP_PHONE_ID",
        "whatsapp_owner": "AIDON_WHATSAPP_OWNER",
        "threads_account_id": "AIDON_THREADS_ACCOUNT_ID",
        "ig_user_id": "AIDON_IG_USER_ID",
        "approval_nudge_channel": "AIDON_APPROVAL_NUDGE_CHANNEL",
        "autonomy_threshold": "AIDON_AUTONOMY_THRESHOLD",
        "autonomy_decay_days": "AIDON_AUTONOMY_DECAY_DAYS",
        "corrected_subject": "AIDON_CORRECTED_SUBJECT",
        "model_think": "AIDON_MODEL_THINK",
        "model_num_ctx": "AIDON_MODEL_NUM_CTX",
        "fast_num_ctx": "AIDON_FAST_NUM_CTX",
        "research_allowlist": "AIDON_RESEARCH_ALLOWLIST",
        "router_fast_phases": "AIDON_ROUTER_FAST_PHASES",
        "kernel": "AIDON_KERNEL",
        "optional_tools": "AIDON_OPTIONAL_TOOLS",
        "tool_posture": "AIDON_TOOL_POSTURE",
        "computer_driver": "AIDON_COMPUTER_DRIVER",
        # Historical spelling remains writable for old installations.
        # New configuration uses the canonical Slack variable below.
        "AIDON_SLACK_SIGNING_SECRET": "AIDON_SLACK_SIGNING_SECRET",
    }
    _ENV_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CLIENT")

    # Settings catalog: every live Config key, grouped, with type,
    # default, and secret flag. Legacy Meta keys and AIDON_DEPTH are
    # absent. Verifier model and think phases remain live because they
    # are needed when using local model backends. Float-valued keys use type "string" (the
    # catalog has only string/int/bool/enum) and are validated
    # as numbers on write. Secrets are listed so the catalog
    # read can mask them and report is_set, never the value.
    SETTINGS_CATALOG = [
        {"group": "Interface", "settings": [
            {"key": "AIDON_WEBUI_TOKEN", "label": "WebUI token", "type": "string", "default": "", "desc": "Bearer token for the WebUI API.", "secret": True},
            {"key": "AIDON_WEBUI_PORT", "label": "WebUI port", "type": "int", "default": "8765", "desc": "Port used by launch.sh when starting the WebUI.", "secret": False},
            {"key": "AIDON_WEBUI_ALLOWED_HOSTS", "label": "WebUI allowed hosts", "type": "string", "default": "", "desc": "Additional Host names accepted by the WebUI.", "secret": False},
            {"key": "AIDON_VERSION", "label": "Displayed version override", "type": "string", "default": "", "desc": "Displayed release version override; empty = installed package version.", "secret": False},
            {"key": "AIDON_VRM_PATH", "label": "Avatar VRM path", "type": "string", "default": "", "desc": "Optional absolute path to the avatar VRM model.", "secret": False},
        ]},
        {"group": "Scheduler", "settings": [
            {"key": "AIDON_DAILY_REPORT_TIME", "label": "Daily report time", "type": "string", "default": "", "desc": "Time of day for the daily report.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_MIN", "label": "Approval nudge minutes", "type": "int", "default": "15", "desc": "Minutes before an approval card is nudged.", "secret": False},
        ]},
        {"group": "System", "settings": [
            {"key": "AIDON_APPROVAL_TIMEOUT_S", "label": "Approval timeout seconds", "type": "int", "default": "900", "desc": "Seconds an approval card waits before failing closed.", "secret": False},
            {"key": "AIDON_EXECUTION_BACKEND", "label": "Execution backend", "type": "enum", "default": "bubblewrap", "desc": "Sandbox backend for shell steps: bubblewrap (local isolation), docker (disposable container), subprocess (no isolation), vm (SSH to a VM you run; needs AIDON_VM_HOST), or modal (Modal cloud sandboxes; needs AIDON_MODAL_APP and `modal setup` on first use). The planner never chooses this; only the owner sets it. Default: bubblewrap.", "secret": False, "options": ["bubblewrap", "docker", "subprocess", "vm", "modal"]},
            {"key": "AIDON_DOCKER_IMAGE", "label": "Docker image", "type": "string", "default": "python:3.12-slim", "desc": "Pinned image for the Docker backend.", "secret": False},
            {"key": "AIDON_DOCKER_NETWORK", "label": "Docker network", "type": "bool", "default": "0", "desc": "Allow network for the Docker backend.", "secret": False},
            {"key": "AIDON_VM_HOST", "label": "VM host", "type": "string", "default": "", "desc": "Hostname or IP of the VM the VM backend SSHes into. Only used when the execution backend is vm; without it the VM backend refuses to run. Default: empty.", "secret": False},
            {"key": "AIDON_VM_USER", "label": "VM user", "type": "string", "default": "", "desc": "SSH user for the VM backend. Only used when the execution backend is vm; empty means your current login. Default: empty.", "secret": False},
            {"key": "AIDON_VM_USE_SUDO", "label": "VM sudo", "type": "bool", "default": "0", "desc": "Run VM commands under sudo -S inside the VM, so runs that need system access work there instead of on the host. Needs AIDON_VM_SUDO_PASSWORD; without it, sudo runs fail closed instead of prompting. Only affects the VM backend. Default: off (0).", "secret": False},
            {"key": "AIDON_VM_SUDO_PASSWORD", "label": "VM sudo password", "type": "string", "default": "", "desc": "Sudo password inside the VM, sent on stdin to sudo -S only; never in a command line, log, or error message. Only used when VM sudo is on. Leave empty to keep the saved value unchanged. Default: empty.", "secret": True},
            {"key": "AIDON_MAX_SANDBOX_SECONDS", "label": "Sandbox timeout seconds", "type": "int", "default": "120", "desc": "Timeout for one sandbox command.", "secret": False},
            {"key": "AIDON_SANDBOX_MEM_MB", "label": "Sandbox memory MB", "type": "int", "default": "1024", "desc": "Maximum address space for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_NPROC", "label": "Sandbox process limit", "type": "int", "default": "256", "desc": "Maximum processes for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_FSIZE_MB", "label": "Sandbox file-size MB", "type": "int", "default": "512", "desc": "Maximum file size created by a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_CPU_S", "label": "Sandbox CPU seconds", "type": "int", "default": "0", "desc": "CPU limit for a sandbox child; 0 disables this limit.", "secret": False},
            {"key": "AIDON_SANDBOX_BACKEND", "label": "Sandbox backend", "type": "enum", "default": "auto", "desc": "Isolation backend selection.", "secret": False, "options": ["auto", "bubblewrap", "subprocess"]},
            {"key": "AIDON_SANDBOX_NETWORK", "label": "Sandbox network", "type": "bool", "default": "0", "desc": "Allow network access inside sandbox commands.", "secret": False},
            {"key": "AIDON_SANDBOX_REQUIRE_ISOLATION", "label": "Require isolation", "type": "bool", "default": "0", "desc": "Refuse sandbox commands when bubblewrap isolation is unavailable.", "secret": False},
            {"key": "AIDON_KERNEL", "label": "Kernel mode", "type": "bool", "default": "0", "desc": "Kernel mode: write/read/list/shell only, no calculate, no plugins.", "secret": False},
            {"key": "AIDON_TOOL_POSTURE", "label": "Tool posture", "type": "enum", "default": "selfmod", "desc": "How far the self-development ladder is open: locked (seed only, no adoption), standard (pure plugins only), developer (capability plugins: http, fs, run, secrets), selfmod (developer plus source changes shown in full and gated by explicit owner approval). Source changes are limited to existing Python files under src/ and backed up before replacement.", "secret": False, "options": ["locked", "standard", "developer", "selfmod"]},
            {"key": "AIDON_OPTIONAL_TOOLS", "label": "Optional tools", "type": "bool", "default": "0", "desc": "Show the full built-in toolset (web search, documents, social, media, email, webcam, YouTube). Off: only the minimal core ladder tools (fetch, ask, installs, MCP connect, plugin adoption). Adopted plugins and trusted skills are never gated. Default: off (0).", "secret": False},
            {"key": "AIDON_COMPUTER_DRIVER", "label": "Computer driver", "type": "enum", "default": "", "desc": "Opt-in desktop driver; requires a graphical session and kernel mode off.", "secret": False, "options": ["", "x11"]},
            {"key": "AIDON_COMPUTER_SECRETS", "label": "Computer secrets", "type": "string", "default": "", "desc": "Comma-separated env names the owner authorizes for computer use. Only listed names resolve; the model names a handle, never a value.", "secret": True},
            {"key": "AIDON_SLACK_AUTOSTART", "label": "Slack polling autostart", "type": "bool", "default": "0", "desc": "Start Slack polling automatically with the WebUI.", "secret": False},
            {"key": "AIDON_AUTONOMY_ENABLED", "label": "Autonomy enabled", "type": "bool", "default": "1", "desc": "Trust-based autonomy for repeated skills.", "secret": False},
            {"key": "AIDON_AUTONOMY_THRESHOLD", "label": "Autonomy threshold", "type": "int", "default": "5", "desc": "Successes before a skill graduates a trust tier.", "secret": False},
            {"key": "AIDON_AUTONOMY_DECAY_DAYS", "label": "Autonomy decay days", "type": "string", "default": "14", "desc": "Days after which trust decays.", "secret": False},
            {"key": "AIDON_TIMEZONE", "label": "Timezone", "type": "string", "default": "UTC", "desc": "Timezone for schedules and reports.", "secret": False},
            {"key": "AIDON_MODAL_APP", "label": "Modal app", "type": "string", "default": "aidon-sandbox", "desc": "Modal app name for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_MODAL_CPU", "label": "Modal CPUs", "type": "string", "default": "1.0", "desc": "CPUs per Modal sandbox for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_MODAL_MEMORY_MB", "label": "Modal memory MB", "type": "int", "default": "1024", "desc": "Memory in MB per Modal sandbox for the Modal execution backend. Only used when the execution backend is modal.", "secret": False},
            {"key": "AIDON_ACQ_MAX_TOOLS", "label": "Acquisition max tools", "type": "int", "default": "2", "desc": "Maximum capability tools acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_SKILLS", "label": "Acquisition max skills", "type": "int", "default": "2", "desc": "Maximum skills acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_MCP", "label": "Acquisition max MCP servers", "type": "int", "default": "1", "desc": "Maximum MCP servers acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_DEPS", "label": "Acquisition max dependency sets", "type": "int", "default": "2", "desc": "Maximum dependency sets acquired per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_REPAIRS", "label": "Acquisition max repair cycles", "type": "int", "default": "3", "desc": "Maximum acquisition repair cycles per run.", "secret": False},
            {"key": "AIDON_ACQ_MAX_SECONDS", "label": "Acquisition max seconds", "type": "string", "default": "600", "desc": "Maximum seconds of acquisition time per run (soft limit, checked between acquisitions).", "secret": False},
            {"key": "AIDON_HUB_TOKEN", "label": "Hub token", "type": "string", "default": "", "desc": "Token for the capability hub client (aidon hub list/push). Empty = unauthenticated.", "secret": True},
        ]},
        {"group": "Cognitive", "settings": [
            {"key": "AIDON_MODEL", "label": "Chat model", "type": "string", "default": "muse-spark", "desc": "Model id for the agent loop.", "secret": False},
            {"key": "AIDON_BACKEND", "label": "Model backend", "type": "enum", "default": "", "desc": "meta, native, http, or fake.", "secret": False, "options": ["meta", "native", "http", "fake"]},
            {"key": "AIDON_API_ENDPOINT", "label": "API endpoint", "type": "string", "default": "", "desc": "Model API endpoint; leave empty until a backend is selected.", "secret": False},
            {"key": "AIDON_API_KEY", "label": "API key", "type": "string", "default": "", "desc": "Key for the Meta Model API or other endpoint.", "secret": True},
            {"key": "AIDON_FAST_MODEL", "label": "Fast model", "type": "string", "default": "", "desc": "Small model for fast phases, else the main model.", "secret": False},
            {"key": "AIDON_FAST_API_ENDPOINT", "label": "Fast model endpoint", "type": "string", "default": "", "desc": "Endpoint for the fast model; empty uses the main API endpoint.", "secret": False},
            {"key": "AIDON_FAST_API_KEY", "label": "Fast model API key", "type": "string", "default": "", "desc": "API key for the fast model endpoint; empty uses the main API key.", "secret": True},
            {"key": "AIDON_FAST_NUM_CTX", "label": "Fast model context", "type": "int", "default": "0", "desc": "Context window for the fast model, 0 = backend default.", "secret": False},
            {"key": "AIDON_MODEL_THINK", "label": "Model thinking", "type": "bool", "default": "0", "desc": "Enable server-side thinking when supported.", "secret": False},
            {"key": "AIDON_MODEL_NUM_CTX", "label": "Model context", "type": "int", "default": "0", "desc": "Context window for the native backend, 0 = server default.", "secret": False},
            {"key": "AIDON_MODEL_TIMEOUT_S", "label": "Model timeout seconds", "type": "string", "default": "120", "desc": "Timeout for one model call, in seconds.", "secret": False},
            {"key": "AIDON_ROUTER_FAST_PHASES", "label": "Router fast phases", "type": "string", "default": "interpret,learn,respond", "desc": "Comma-separated phases routed to the fast model.", "secret": False},
            {"key": "AIDON_IMAGE_MODEL", "label": "Image model", "type": "string", "default": "muse-image-1.0", "desc": "Image generation model.", "secret": False},
            {"key": "AIDON_SAM_MODEL", "label": "SAM model", "type": "string", "default": "meta-sam", "desc": "Segmentation model for video editing.", "secret": False},
            {"key": "AIDON_SAM_BASE_URL", "label": "SAM base URL", "type": "string", "default": "https://api.meta.ai/v1", "desc": "Meta Model API base URL for SAM.", "secret": False},
            {"key": "AIDON_SAM_API_KEY", "label": "SAM API key", "type": "string", "default": "", "desc": "SAM key; empty uses the main API key.", "secret": True},
            {"key": "AIDON_VERIFIER_MODEL", "label": "Verifier model", "type": "string", "default": "", "desc": "Independent model for verification; empty uses the main model.", "secret": False},
            {"key": "AIDON_VERIFY_SEED", "label": "Verifier seed", "type": "string", "default": "42", "desc": "Deterministic verify seed; leave empty to disable.", "secret": False},
            {"key": "AIDON_VISION_TIERS", "label": "Vision tiers", "type": "string", "default": "strong", "desc": "Comma-separated model tiers allowed to receive images.", "secret": False},
            {"key": "AIDON_THINK_PHASES", "label": "Thinking phases", "type": "string", "default": "interpret,verify", "desc": "Comma-separated phases that enable model thinking; empty disables phase overrides.", "secret": False},
            {"key": "AIDON_DYNAMIC_SAMPLING", "label": "Dynamic sampling", "type": "bool", "default": "1", "desc": "Per-phase temperature/top_p .env overrides apply; 0 = built-in sampling table only.", "secret": False},
            {"key": "AIDON_TEMP_RESPOND", "label": "Temperature (respond)", "type": "string", "default": "", "desc": "Respond-phase temperature override; empty = built-in 0.8.", "secret": False},
            {"key": "AIDON_TOP_P_RESPOND", "label": "Top_p (respond)", "type": "string", "default": "", "desc": "Respond-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_WEBCHAT", "label": "Temperature (webchat)", "type": "string", "default": "", "desc": "Webchat-phase temperature override; empty = built-in 0.2.", "secret": False},
            {"key": "AIDON_TOP_P_WEBCHAT", "label": "Top_p (webchat)", "type": "string", "default": "", "desc": "Webchat-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_PLAN", "label": "Temperature (plan)", "type": "string", "default": "", "desc": "Plan-phase temperature override; empty = built-in 0.2.", "secret": False},
            {"key": "AIDON_TOP_P_PLAN", "label": "Top_p (plan)", "type": "string", "default": "", "desc": "Plan-phase top_p override; empty = built-in 0.95.", "secret": False},
            {"key": "AIDON_TEMP_VERIFY", "label": "Temperature (verify)", "type": "string", "default": "", "desc": "Verify-phase temperature override; empty = built-in 0.0.", "secret": False},
            {"key": "AIDON_TOP_P_VERIFY", "label": "Top_p (verify)", "type": "string", "default": "", "desc": "Verify-phase top_p override; empty = built-in 0.9.", "secret": False},
            {"key": "AIDON_EFFORT_RESPOND", "label": "Thinking effort (respond)", "type": "enum", "default": "", "desc": "Respond-phase thinking effort pin; empty = built-in off. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_WEBCHAT", "label": "Thinking effort (webchat)", "type": "enum", "default": "", "desc": "Webchat-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_PLAN", "label": "Thinking effort (plan)", "type": "enum", "default": "", "desc": "Plan-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_EFFORT_VERIFY", "label": "Thinking effort (verify)", "type": "enum", "default": "", "desc": "Verify-phase thinking effort pin; empty = built-in low. A pin is never raised by escalation.", "secret": False, "options": ["", "off", "low", "medium", "high"]},
            {"key": "AIDON_ROUTER", "label": "Intent router", "type": "enum", "default": "semantic", "desc": "Intent router: semantic (local routing) or clef (Clef decision model; needs AIDON_CLEF_URL). Default: semantic.", "secret": False, "options": ["semantic", "clef"]},
            {"key": "AIDON_CLEF_URL", "label": "Clef URL", "type": "string", "default": "", "desc": "Clef decision-model endpoint URL. Empty = no Clef anywhere.", "secret": False},
            {"key": "AIDON_CLEF_MODEL", "label": "Clef model", "type": "string", "default": "Cloudflare/clef-flash", "desc": "Clef model id.", "secret": False},
            {"key": "AIDON_CLEF_KEY", "label": "Clef key", "type": "string", "default": "", "desc": "Clef proxy-auth key, only when the endpoint requires proxy auth.", "secret": True},
            {"key": "AIDON_CLEF_SECRET", "label": "Clef secret", "type": "string", "default": "", "desc": "Clef proxy-auth secret, only when the endpoint requires proxy auth.", "secret": True},
        ]},
        {"group": "Memory", "settings": [
            {"key": "AIDON_EMBED_MODEL", "label": "Embed model", "type": "string", "default": "google/embeddinggemma-2", "desc": "Embedding model for memory.", "secret": False},
            {"key": "AIDON_EMBEDDER", "label": "Embedder", "type": "enum", "default": "", "desc": "Embedder selection: empty = EmbeddingGemma 2 440M (with AIDON_EMBED_MODEL override), hash = offline hash embedder for tests.", "secret": False, "options": ["", "hash"]},
            {"key": "AIDON_SPARSE_MODEL", "label": "Sparse memory model", "type": "string", "default": "prithivida/Splade_PP_en_v1", "desc": "Learned sparse model for hybrid memory recall; empty uses keyword sparse weights.", "secret": False},
            {"key": "AIDON_EMBEDDING_REVISION", "label": "Embed model revision", "type": "string", "default": "f9972ac22e696a105c482202646979e33e4b5a1e", "desc": "Pinned snapshot revision of the embedding model. Upstream README edits otherwise mint a new snapshot directory on every HEAD move, which looks like the weights downloading again. Override with another revision hash to use a different snapshot. Default: f9972ac22e696a105c482202646979e33e4b5a1e.", "secret": False},
            {"key": "AIDON_FORGET_HALF_LIFE_DAYS", "label": "Forget half-life days", "type": "string", "default": "21", "desc": "Forgetting-curve half-life, in days.", "secret": False},
            {"key": "AIDON_FORGET_GRACE_DAYS", "label": "Forget grace days", "type": "int", "default": "35", "desc": "Days before forgetting starts.", "secret": False},
            {"key": "AIDON_FORGET_CLEANUP_THRESHOLD", "label": "Forget cleanup threshold", "type": "string", "default": "0.02", "desc": "Recall weight below which a point is pruned.", "secret": False},
            {"key": "AIDON_EPISODE_RECALL_LIMIT", "label": "Episode recall limit", "type": "int", "default": "1", "desc": "Number of prior experiences considered before a fresh plan. Composition always uses at least 5; that floor is fixed and cannot go lower.", "secret": False},
            {"key": "AIDON_KNOWLEDGE_RECALL_LIMIT", "label": "Knowledge recall limit", "type": "int", "default": "3", "desc": "Number of knowledge points considered before planning.", "secret": False},
            {"key": "AIDON_RECALL_MAX_TOKENS", "label": "Recall token budget", "type": "int", "default": "2000", "desc": "Maximum memory context included in a plan.", "secret": False},
            {"key": "AIDON_LEARN_MAX_SKILLS_PER_DAY", "label": "Learning skills per day", "type": "int", "default": "5", "desc": "Maximum candidate skills promoted by one learn cycle per day.", "secret": False},
            {"key": "AIDON_SKILL_MAX_FAILURES", "label": "Skill failure limit", "type": "int", "default": "3", "desc": "Failures before a skill is retired.", "secret": False},
            {"key": "AIDON_INGEST_DIR", "label": "Ingest directory", "type": "string", "default": "", "desc": "Folder watched for ingest triggers.", "secret": False},
            {"key": "AIDON_QDRANT_URL", "label": "Qdrant URL", "type": "string", "default": "", "desc": "Qdrant server URL, empty = local mode.", "secret": False},
            {"key": "AIDON_MEMORY_EXTRACT", "label": "Background fact extraction", "type": "int", "default": "1", "desc": "Extract facts and understandings in the background after chats and runs; 0 disables it.", "secret": False},
            {"key": "AIDON_DAG_SIMILARITY", "label": "DAG similarity threshold", "type": "string", "default": "0.3", "desc": "Goal-shape overlap (0-1) for DAG replacement, experience adaptation, and failure matching; lower merges more aggressively.", "secret": False},
            {"key": "AIDON_LEARN_MAX_TOKENS_PER_DAY", "label": "Learn tokens per day", "type": "int", "default": "500000", "desc": "Tokens the self-improvement loop may spend per UTC day (learn phase only). Default: 500000.", "secret": False},
            {"key": "AIDON_SEMANTIC_RECALL_LIMIT", "label": "Semantic recall limit", "type": "int", "default": "3", "desc": "General-fact (semantic) recall limit for plan prompts. Default: 3.", "secret": False},
            {"key": "AIDON_RECALL_CAP_EPISODES", "label": "Recall cap episodes", "type": "int", "default": "700", "desc": "Per-source cap for episode recall in the fused planning block; one noisy source can't starve the others. Default: 700.", "secret": False},
            {"key": "AIDON_RECALL_CAP_KNOWLEDGE", "label": "Recall cap knowledge", "type": "int", "default": "700", "desc": "Per-source cap for knowledge recall in the fused planning block. Default: 700.", "secret": False},
            {"key": "AIDON_RECALL_CAP_SKILLS", "label": "Recall cap skills", "type": "int", "default": "500", "desc": "Per-source cap for skill recall in the fused planning block. Default: 500.", "secret": False},
        ]},
        {"group": "Voice", "settings": [
            {"key": "AIDON_TTS_VOICE_ID", "label": "TTS voice id", "type": "string", "default": "", "desc": "Voice id for text-to-speech.", "secret": False},
            {"key": "AIDON_VOICE_ANNOUNCE", "label": "Voice announcements", "type": "bool", "default": "0", "desc": "Automatically play spoken WebUI replies.", "secret": False},
            {"key": "AIDON_FISH_API_KEY", "label": "Fish API key", "type": "string", "default": "", "desc": "Key for Fish TTS.", "secret": True},
            {"key": "AIDON_TRANSCRIBE_API_KEY", "label": "Transcribe API key", "type": "string", "default": "", "desc": "Key for voice transcription.", "secret": True},
        ]},
        {"group": "Agentic", "settings": [
            {"key": "AIDON_MAX_TOKENS_PER_RUN", "label": "Max tokens per run", "type": "int", "default": "200000", "desc": "Hard token budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_STEPS", "label": "Max steps", "type": "int", "default": "50", "desc": "Hard step budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_WALLCLOCK_MIN", "label": "Max wallclock minutes", "type": "int", "default": "60", "desc": "Hard wallclock budget for one goal run, in minutes.", "secret": False},
            {"key": "AIDON_PARALLEL_WORKERS", "label": "Parallel DAG workers", "type": "int", "default": "3", "desc": "Max concurrent DAG/plan steps in one level; 1 = sequential.", "secret": False},
            {"key": "AIDON_MAX_STUCK_ITERATIONS", "label": "Max stuck iterations", "type": "int", "default": "8", "desc": "Iterations without progress before the run stops.", "secret": False},
            {"key": "AIDON_PARALLEL_TOOLS", "label": "Parallel tools", "type": "int", "default": "4", "desc": "Parallel research/tool workers.", "secret": False},
            {"key": "AIDON_MAX_ATTEMPTS", "label": "Repair attempts per run", "type": "int", "default": "3", "desc": "Execute, verify, repair, up to this many tries.", "secret": False},
            {"key": "AIDON_MAX_REPLANS", "label": "Research-plus-replan passes", "type": "int", "default": "1", "desc": "Replan passes before execution; 0 skips replanning.", "secret": False},
            {"key": "AIDON_MAX_PLAN_REPAIRS", "label": "Plan repair tries", "type": "int", "default": "2", "desc": "Repair tries when the validator rejects a plan; 0 fails immediately.", "secret": False},
            {"key": "AIDON_REACT_MAX_ROUNDS", "label": "ReAct max rounds", "type": "int", "default": "8", "desc": "Max rounds for the ReAct compatibility loop.", "secret": False},
            {"key": "AIDON_REACT_MAX_WORKERS", "label": "ReAct parallel workers", "type": "int", "default": "3", "desc": "Parallel tool workers within one ReAct round; 1 = sequential.", "secret": False},
            {"key": "AIDON_CRITIC_ENABLED", "label": "Plan critic on/off", "type": "int", "default": "1", "desc": "Reflection pass on each valid plan before execution; 0 skips it.", "secret": False},
            {"key": "AIDON_CRITIC_MAX_ROUNDS", "label": "Critic repair rounds", "type": "int", "default": "1", "desc": "Critic-issue repair rounds; 0 runs the critic without repairing.", "secret": False},
            {"key": "AIDON_CRITIC_MAX_ISSUES", "label": "Critic issues per round", "type": "int", "default": "5", "desc": "Most issues the critic returns per review round; repairs address them in order.", "secret": False},
            {"key": "AIDON_CRITIC_MODE", "label": "Critic mode", "type": "enum", "default": "risk", "desc": "Plan critic scope: risk (review only fresh large plans and plans with live steps; adapted and small plans skip it) or always (review every valid plan). Flip to always if risk-based review misses real plan flaws. Default: risk.", "secret": False, "options": ["risk", "always"]},
            {"key": "AIDON_CRITIC_RISK_MAX_STEPS", "label": "Critic risk small-plan steps", "type": "int", "default": "3", "desc": "A plan with at most this many steps counts as small for risk-based critic mode and skips review when it has no live steps; 0 means only adapted plans skip. Default: 3.", "secret": False},
            {"key": "AIDON_MODEL_VERIFIER", "label": "Model verifier mode", "type": "enum", "default": "off", "desc": "Model second opinion after code verification passes: off (code decides alone, fewest tokens), selective (runs unless every deliverable is structured data such as JSON or CSV, where code checks are complete), or always (runs on every passing run). Flip up if code-only verification misses quality flaws. Default: off.", "secret": False, "options": ["off", "selective", "always"]},
            {"key": "AIDON_CHOICE_POLICY", "label": "Choice policy", "type": "enum", "default": "auto", "desc": "Choice policy when research finds real alternatives: auto (reason and pick) or ask (present ranked candidates at a choice gate). Default: auto.", "secret": False, "options": ["auto", "ask"]},
        ]},
        {"group": "Social", "settings": [
            {"key": "AIDON_WHATSAPP_TOKEN", "label": "WhatsApp token", "type": "string", "default": "", "desc": "WhatsApp Cloud API token.", "secret": True},
            {"key": "AIDON_WHATSAPP_PHONE_ID", "label": "WhatsApp phone id", "type": "string", "default": "", "desc": "WhatsApp Cloud phone number id.", "secret": False},
            {"key": "AIDON_WHATSAPP_VERIFY_TOKEN", "label": "WhatsApp verify token", "type": "string", "default": "", "desc": "Token WhatsApp uses to verify the webhook.", "secret": True},
            {"key": "AIDON_WHATSAPP_APP_SECRET", "label": "WhatsApp app secret", "type": "string", "default": "", "desc": "App secret for WhatsApp signature checks.", "secret": True},
            {"key": "AIDON_WHATSAPP_OWNER", "label": "WhatsApp owner", "type": "string", "default": "", "desc": "Owner phone number for the WhatsApp owner gate.", "secret": False},
            {"key": "SLACK_BOT_TOKEN", "label": "Slack bot token", "type": "string", "default": "", "desc": "Slack bot token for inbound/outbound Slack polling.", "secret": True},
            {"key": "AIDON_SLACK_SIGNING_SECRET", "label": "Slack signing secret", "type": "string", "default": "", "desc": "Required for the Slack webhook; requests are rejected when it is empty.", "secret": True},
            {"key": "AIDON_THREADS_ACCOUNT_ID", "label": "Threads account id", "type": "string", "default": "", "desc": "Threads account id for publishing.", "secret": False},
            {"key": "AIDON_THREADS_ACCESS_TOKEN", "label": "Threads access token", "type": "string", "default": "", "desc": "Threads access token for publishing.", "secret": True},
            {"key": "AIDON_IG_USER_ID", "label": "Instagram user id", "type": "string", "default": "", "desc": "Instagram user id for publishing.", "secret": False},
            {"key": "AIDON_IG_ACCESS_TOKEN", "label": "Instagram access token", "type": "string", "default": "", "desc": "Instagram access token for publishing.", "secret": True},
            {"key": "GITHUB_TOKEN", "label": "GitHub token", "type": "string", "default": "", "desc": "GitHub token for repository tools.", "secret": True},
            {"key": "AIDON_SLACK_INBOUND_CHANNEL", "label": "Slack inbound channel(s)", "type": "string", "default": "", "desc": "Comma-separated Slack channel or DM IDs for polling.", "secret": False},
            {"key": "AIDON_SLACK_OUTBOUND_CHANNEL", "label": "Slack outbound channel", "type": "string", "default": "", "desc": "Slack channel outbound replies go to; empty = the inbound channel.", "secret": False},
            {"key": "AIDON_SLACK_POLL_SECONDS", "label": "Slack poll seconds", "type": "int", "default": "10", "desc": "Polling interval for Slack inbound messages.", "secret": False},
            {"key": "AIDON_SLACK_ACK_AFTER_SECONDS", "label": "Slack acknowledgement delay", "type": "int", "default": "15", "desc": "Seconds before a long-running Slack goal gets an acknowledgement.", "secret": False},
            {"key": "AIDON_OWNER_EMAIL", "label": "Owner email", "type": "string", "default": "", "desc": "Where drafts and reports to the owner are addressed.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_CHANNEL", "label": "Approval nudge channel", "type": "string", "default": "", "desc": "Channel approval nudges go to.", "secret": False},
            {"key": "AIDON_CORRECTED_SUBJECT", "label": "Corrected subject", "type": "bool", "default": "1", "desc": "Mark corrected emails with a Corrected subject.", "secret": False},
        ]},
        {"group": "Research", "settings": [
            {"key": "AIDON_SEARCH_BACKEND", "label": "Search backend", "type": "enum", "default": "tavily", "desc": "Web search backend.", "secret": False, "options": ["tavily", "searxng"]},
            {"key": "AIDON_SEARXNG_URL", "label": "SearXNG URL", "type": "string", "default": "http://localhost:8888", "desc": "SearXNG server for the searxng backend.", "secret": False},
            {"key": "TAVILY_API_KEY", "label": "Tavily API key", "type": "string", "default": "", "desc": "Key for Tavily search.", "secret": True},
            {"key": "AIDON_RESEARCH_MAX_ROUNDS", "label": "Research max rounds", "type": "int", "default": "10", "desc": "Max research rounds per run.", "secret": False},
            {"key": "AIDON_RESEARCH_ALLOWLIST", "label": "Research allowlist", "type": "string", "default": "", "desc": "Comma-separated domains research may use, empty = unrestricted.", "secret": False},
            {"key": "AIDON_YOUTUBE_API_KEY", "label": "YouTube API key", "type": "string", "default": "", "desc": "Key for YouTube search.", "secret": True},
            {"key": "YOUTUBE_CLIENT_ID", "label": "YouTube OAuth client ID", "type": "string", "default": "", "desc": "OAuth client ID for YouTube uploads (youtube.upload scope).", "secret": True},
            {"key": "YOUTUBE_CLIENT_SECRET", "label": "YouTube OAuth client secret", "type": "string", "default": "", "desc": "OAuth client secret for YouTube uploads.", "secret": True},
            {"key": "YOUTUBE_REFRESH_TOKEN", "label": "YouTube OAuth refresh token", "type": "string", "default": "", "desc": "Refresh token from a one-time youtube.upload consent.", "secret": True},
            {"key": "YOUTUBE_PRIVACY_STATUS", "label": "YouTube upload privacy", "type": "enum", "default": "private", "desc": "Default visibility for YouTube uploads.", "secret": False, "options": ["private", "unlisted", "public"]},
            {"key": "YOUTUBE_CATEGORY_ID", "label": "YouTube upload category", "type": "string", "default": "22", "desc": "Category ID for YouTube uploads (22 = People & Blogs).", "secret": False},
            {"key": "AIDON_DEEP_RESEARCH_ROUNDS", "label": "Deep research rounds", "type": "int", "default": "2", "desc": "Max search rounds for the deep_research tool.", "secret": False},
            {"key": "AIDON_DEEP_RESEARCH_SOURCES", "label": "Deep research sources", "type": "int", "default": "5", "desc": "Max pages fetched per deep_research round.", "secret": False},
        ]},
    ]
    _CATALOG_FLOAT_KEYS = frozenset({
        "AIDON_MODEL_TIMEOUT_S", "AIDON_FORGET_HALF_LIFE_DAYS",
        "AIDON_FORGET_CLEANUP_THRESHOLD", "AIDON_AUTONOMY_DECAY_DAYS",
        "AIDON_DAG_SIMILARITY", "AIDON_MODAL_CPU", "AIDON_ACQ_MAX_SECONDS",
    })

    def _catalog_entry(self, env_key: str):
        for group in self.SETTINGS_CATALOG:
            for entry in group["settings"]:
                if entry["key"] == env_key:
                    return entry
        return None

    def _read_env_file(self) -> dict:
        """Parse <home>/.env into {key: value}, no env fallback here."""
        path = self.home / ".env"
        values: dict[str, str] = {}
        if not path.is_file():
            return values
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return values
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.startswith("export "):
                stripped = stripped[7:].strip()
            key, value = stripped.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            if key:
                values[key] = value
        return values

    def settings_catalog(self) -> dict:
        """Catalog + current values. Secrets come back masked.

    A secret entry returns a fixed ``****`` mask when set and
    is_set, never the value itself. A non-secret returns its .env value, else
        its process-environment value, else its default, with
        from_file saying whether .env set it.
        """
        import os
        # Only expose a slider when the bound is meaningful and safe for
        # the setting.  Unbounded or deployment-specific values remain
        # ordinary inputs in the Settings page.
        bounds = {
            "AIDON_FAST_NUM_CTX": (0, 262144, 1024),
            "AIDON_MODEL_NUM_CTX": (0, 262144, 1024),
            "AIDON_MAX_TOKENS_PER_RUN": (1000, 1000000, 1000),
            "AIDON_MAX_STEPS": (1, 200, 1),
            "AIDON_MAX_WALLCLOCK_MIN": (1, 1440, 1),
            "AIDON_APPROVAL_TIMEOUT_S": (30, 3600, 30),
            "AIDON_MAX_SANDBOX_SECONDS": (1, 3600, 1),
            "AIDON_SANDBOX_MEM_MB": (0, 32768, 128),
            "AIDON_SANDBOX_NPROC": (0, 4096, 1),
            "AIDON_SANDBOX_FSIZE_MB": (0, 102400, 512),
            "AIDON_SANDBOX_CPU_S": (0, 86400, 60),
            "AIDON_FORGET_GRACE_DAYS": (0, 3650, 1),
            "AIDON_EPISODE_RECALL_LIMIT": (0, 100, 1),
            "AIDON_KNOWLEDGE_RECALL_LIMIT": (0, 100, 1),
            "AIDON_RECALL_MAX_TOKENS": (0, 100000, 100),
            "AIDON_LEARN_MAX_SKILLS_PER_DAY": (0, 100, 1),
            "AIDON_SKILL_MAX_FAILURES": (0, 100, 1),
            "AIDON_RESEARCH_MAX_ROUNDS": (0, 100, 1),
            "AIDON_DEEP_RESEARCH_ROUNDS": (1, 5, 1),
            "AIDON_DEEP_RESEARCH_SOURCES": (1, 10, 1),
            "AIDON_MAX_STUCK_ITERATIONS": (0, 100, 1),
            "AIDON_PARALLEL_TOOLS": (1, 32, 1),
            "AIDON_PARALLEL_WORKERS": (1, 32, 1),
            "AIDON_MAX_ATTEMPTS": (1, 100, 1),
            "AIDON_MAX_REPLANS": (0, 10, 1),
            "AIDON_MAX_PLAN_REPAIRS": (0, 10, 1),
            "AIDON_REACT_MAX_ROUNDS": (1, 100, 1),
            "AIDON_REACT_MAX_WORKERS": (1, 32, 1),
            "AIDON_CRITIC_ENABLED": (0, 1, 1),
            "AIDON_CRITIC_MAX_ROUNDS": (0, 5, 1),
            "AIDON_CRITIC_MAX_ISSUES": (1, 20, 5),
            "AIDON_CRITIC_RISK_MAX_STEPS": (0, 20, 3),
            "AIDON_MEMORY_EXTRACT": (0, 1, 1),
            "AIDON_SLACK_POLL_SECONDS": (3, 3600, 1),
            "AIDON_SLACK_ACK_AFTER_SECONDS": (0, 3600, 1),
            "AIDON_APPROVAL_NUDGE_MIN": (0, 1440, 1),
            "AIDON_AUTONOMY_THRESHOLD": (0, 100, 1),
            "AIDON_WEBUI_PORT": (1024, 65535, 1),
        }
        file_vals = self._read_env_file()
        groups = []
        for group in self.SETTINGS_CATALOG:
            items = []
            for entry in group["settings"]:
                item = {k: entry[k] for k in
                        ("key", "label", "type", "default", "desc", "secret")
                        if k in entry}
                if "options" in entry:
                    item["options"] = list(entry["options"])
                if entry.get("type") == "int" and entry["key"] in bounds:
                    item["min"], item["max"], item["step"] = bounds[entry["key"]]
                raw = file_vals.get(entry["key"])
                if raw is None:
                    raw = os.environ.get(entry["key"])
                item["from_file"] = entry["key"] in file_vals
                if entry.get("secret"):
                    item["is_set"] = bool(raw)
                    item["value"] = "****" if item["is_set"] else ""
                else:
                    item["value"] = raw if raw is not None else entry.get("default", "")
                items.append(item)
            groups.append({"group": group["group"], "settings": items})
        return {"env_file": str(self.home / ".env"), "groups": groups}

    def settings_set_env(self, key: str, value) -> dict:
        """Write one setting to <home>/.env, secrets included.

        The settings page can enter
        everything, keys included. Accepts the friendly settings
        name (e.g. "timezone") or the env var name itself (e.g.
        "AIDON_TIMEZONE"), for every catalog key plus the original
        19 friendly names. Type validation: int must parse as int,
        bool must be one of 0/1/true/false/yes/no (stored as 1/0),
        enum must be in options, float-valued keys must parse as a
        number. A secret key with an empty value is a no-op
        ("don't change") and must never clear the key; a secret
        with a value is written, write-only — the catalog never
        returns it back (value "****" plus is_set when set). Unknown keys and
        newline values are refused. The value is never printed or
        returned: the result carries only the key name.
        The .env file is created if missing and kept owner-only.
        """
        raw_key = str(key or "").strip()
        env_key = self._ENV_SETTING_MAP.get(raw_key) or raw_key
        entry = self._catalog_entry(env_key)
        if entry is None and env_key not in self._ENV_SETTING_MAP.values():
            return {"ok": False, "error": f"setting not writable to .env: {raw_key}"}
        is_secret = bool(entry is not None and entry.get("secret")) or any(
            marker in env_key for marker in self._ENV_SECRET_MARKERS)
        if is_secret and str(value) == "":
            return {"ok": True, "key": env_key, "unchanged": True}
        text = str(value).strip() if not isinstance(value, str) else value.strip()
        if "\n" in text or "\r" in text:
            return {"ok": False, "error": "value must not contain a newline"}
        if entry is not None:
            kind = entry.get("type", "string")
            if kind == "int":
                try:
                    text = str(int(text))
                except ValueError:
                    return {"ok": False, "error": f"{env_key}: not an integer: {value!r}"}
            elif kind == "bool":
                lowered = text.lower()
                if lowered not in ("0", "1", "true", "false", "yes", "no"):
                    return {"ok": False, "error": f"{env_key}: not a boolean: {value!r}"}
                text = "1" if lowered in ("1", "true", "yes") else "0"
            elif kind == "enum":
                if text not in (entry.get("options") or []):
                    return {"ok": False, "error": f"{env_key}: must be one of {entry.get('options')}"}
            elif env_key in self._CATALOG_FLOAT_KEYS:
                try:
                    float(text)
                except ValueError:
                    return {"ok": False, "error": f"{env_key}: not a number: {value!r}"}
        path = self.home / ".env"
        lines: list[str] = []
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
        out: list[str] = []
        found = False
        for line in lines:
            stripped = line.strip()
            candidate = stripped[7:].strip() if stripped.startswith("export ") else stripped
            if "=" in candidate and candidate.split("=", 1)[0].strip() == env_key and not found:
                out.append(f"{env_key}={text}")
                found = True
            else:
                out.append(line)
        if not found:
            out.append(f"{env_key}={text}")
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return {"ok": True, "key": env_key}

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
        from ..capabilities.plugins import PluginRegistry
        registry = PluginRegistry(self.home)
        registry.load_all()
        gate = None if approve is not None else getattr(self.webui_gate, "gate", None)
        if approve is None and gate is None:
            return {"ok": False, "error": "adoption needs approval"}
        ok, findings, message = registry.adopt(source, gate=gate, approve=approve)
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

    # -- Additional WebUI surfaces: tools, conversations, briefs, ideas, hub runs,
    #    reports, TTS, and goals with images. These are new routes only;
    #    they do not change any existing route or page above. ----------
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
                       or "standard").lower() == "selfmod"
                   and not getattr(self.config, "kernel_mode", False))
        goal_tools = list(dict.fromkeys(
            all_tool_names(plugin_tools, selfmod=selfmod) + live_names))
        return {"sandbox_tools": ["write", "read", "list", "shell", "calculate"],
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
            record = db.create_conversation()
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
                return {"id": conversation_id, "turns": history}
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
            goal += ("\n\nAttached image paths (pass one to edit_image when a "
                     "style change or edit is requested):\n" + "\n".join(image_paths))
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

    def handle_dag_layout(self, body: dict) -> tuple[int, dict]:
        """Layered layout for a DAG, owned by the server so the
        editor and any caller agree on where a node goes.

        Body: {"dag": {"nodes": [...]}} (a bare list of nodes, or a
        dag dict with "steps" instead of "nodes", is also accepted).
        layer = longest-path depth from roots via depends_on
        (roots layer 0); x = layer * 200, y = index within the
        layer * 90. Entries follow topological order. An invalid
        or missing dag, an empty node id, an unknown dependency,
        or a cycle is 400, never a half-layout.
        """
        from ..agentic.dag import DAG, DAGNode
        dag_data = body.get("dag", {}) if isinstance(body, dict) else {}
        if isinstance(dag_data, list):
            raw_nodes = dag_data
        elif isinstance(dag_data, dict):
            raw_nodes = dag_data.get("nodes", dag_data.get("steps"))
        else:
            raw_nodes = None
        if not isinstance(raw_nodes, list) or not raw_nodes:
            return 400, {"error": "dag object with nodes required"}
        nodes = []
        for item in raw_nodes:
            if not isinstance(item, dict) or not str(item.get("id", "")):
                return 400, {"error": "every node needs a non-empty id"}
            try:
                nodes.append(DAGNode.from_dict(item))
            except Exception as exc:
                return 400, {"error": f"invalid node: {exc}"}
        dag = DAG(nodes)
        failures = dag.validate()
        if failures:
            return 400, {"error": "; ".join(failures)[:500]}
        layers: dict[str, int] = {}
        for node in dag.topological_order():
            deps = [d for d in node.depends_on if d in layers]
            layers[node.id] = (1 + max(layers[d] for d in deps)) if deps else 0
        per_layer: dict[int, int] = {}
        layout = []
        for node in dag.topological_order():
            layer = layers[node.id]
            index = per_layer.get(layer, 0)
            per_layer[layer] = index + 1
            layout.append({"id": node.id, "x": layer * 200,
                           "y": index * 90, "layer": layer})
        return 200, {"layout": layout}

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

    def handle_reports_list(self) -> dict:
        directory = self._json_dir("reports")
        days = sorted(path.stem for path in directory.glob("*.md"))
        return {"reports": days}

    def handle_report_get(self, day: str):
        """Return {"day", "content"} for a stored report, or None
        when no report exists for that day. The day is sanitised
        with _safe_name, so a traversal cannot escape reports/.
        """
        path = self._json_dir("reports") / f"{self._safe_name(day)}.md"
        if not path.is_file():
            return None
        return {"day": self._safe_name(day),
                "content": path.read_text(encoding="utf-8")}

    def handle_missions_list(self) -> dict:
        """Mission records stored under <home>/missions/<conversation>/
        mission.json. No missions is an empty list, never an error;
        a directory without a mission.json is skipped.
        """
        records = []
        missions_dir = self.home / "missions"
        if missions_dir.is_dir():
            for mission_dir in sorted(missions_dir.iterdir()):
                if not mission_dir.is_dir():
                    continue
                data = self._read_json_file(mission_dir / "mission.json")
                if data is not None:
                    records.append(data)
        return {"missions": records}

    def handle_mission_get(self, mission_id: str):
        from ..capabilities.mission import MissionStore
        try:
            return MissionStore(self.home).get(self._safe_name(mission_id))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def handle_mission_delete(self, mission_id: str) -> dict:
        import shutil
        safe = self._safe_name(mission_id)
        path = self.home / "missions" / safe
        if not path.is_dir():
            return {"ok": False}
        shutil.rmtree(path)
        return {"ok": True}

    def handle_capabilities(self) -> dict:
        """One view over what Aidon can do: sandbox + plugin tool
        names, skill library names, and MCP server names. A source
        that is missing or unreadable contributes an empty list,
        never an error, so the route works on a fresh home.
        """
        try:
            tools = self.handle_tools()
            tool_names = list(tools.get("goal_tools", []))
            plugin_names = set(tools.get("plugin_tools", []))
        except Exception:
            tool_names = []
            plugin_names = set()
        side_effect_tools = {
            "schedule_job", "cancel_schedule", "slack_send", "whatsapp_send",
            "threads_publish", "threads_post", "instagram_publish",
            "instagram_post", "email_send", "record_system_audio", "notify",
            "uv_venv", "install_python_package", "apt_install",
            "adopt_tool_plugin", "mcp_connect", "mcp_disconnect", "self_modify", "tts_speak",
            "write_file", "write_versioned", "sam_segment",
        }
        tool_rows = []
        for name in tool_names:
            tool_rows.append({
                "name": name,
                "description": ("Adopted plugin tool." if name in plugin_names
                                 else "Goal-run tool."),
                "origin": "plugin" if name in plugin_names else "core",
                "side_effect": name in side_effect_tools,
            })
        try:
            from ..capabilities.skill_library import SkillLibrary
            skill_names = [skill.name for skill in SkillLibrary(self.home).list()]
        except Exception:
            skill_names = []
        mcp_servers: list[str] = []
        try:
            data = self._read_json_file(self.home / "mcp_servers.json")
            if isinstance(data, dict):
                mcp_servers = [str(key) for key in data]
            elif isinstance(data, list):
                mcp_servers = [str(item.get("name", item)) if isinstance(item, dict)
                               else str(item) for item in data]
        except Exception:
            mcp_servers = []
        return {"tools": tool_rows, "skills": skill_names,
                "mcp_servers": mcp_servers}

    def handle_capability_code(self, name: str):
        """Python source for a named capability handler, or None
        when no importable handler carries that name.

        The search covers module-level callables in the
        capabilities and agentic packages (the handlers a plan
        step can actually name). Extra-tool closures built
        inside main._goal_extra_tools are not importable from
        the server — it holds no config — so they are not
        listed here; a name that only exists as such a closure
        is a 404, not a wrong source.
        """
        import importlib
        import inspect
        import pkgutil
        wanted = str(name or "")
        if not wanted:
            return None
        # Goal-run tools are closures assembled by main.py, so they do not
        # exist as importable module-level functions. They are nevertheless
        # real tools; show their builder source instead of returning a
        # misleading 404 from the capabilities studio.
        try:
            from ..capabilities.tool_catalog import all_tool_names
            selfmod = (str(getattr(self.config, "tool_posture", "standard")
                           or "standard").lower() == "selfmod"
                       and not getattr(self.config, "kernel_mode", False))
            if wanted in all_tool_names(selfmod=selfmod):
                if wanted in {"write", "read", "list", "shell", "calculate"}:
                    source_module = importlib.import_module("src.capabilities.tools")
                    source = inspect.getsource(source_module.ToolRegistry)
                    note = "Built-in sandbox tool; implementation is shown from ToolRegistry."
                else:
                    source_module = importlib.import_module("src.system.main")
                    source = inspect.getsource(source_module._goal_extra_tools)
                    note = "Goal tool assembled by _goal_extra_tools; implementation is shown from its builder."
                return {"name": wanted, "code": source, "origin": "core", "note": note}
        except (ImportError, OSError, TypeError):
            pass
        for package in ("src.capabilities", "src.agentic"):
            try:
                pkg = importlib.import_module(package)
            except Exception:
                continue
            for info in pkgutil.iter_modules(pkg.__path__):
                try:
                    module = importlib.import_module(f"{package}.{info.name}")
                except Exception:
                    continue
                candidate = getattr(module, wanted, None)
                if callable(candidate):
                    try:
                        return {"name": wanted,
                                "code": inspect.getsource(candidate)}
                    except (OSError, TypeError):
                        continue
        return None

    def handle_memory_points(self, collection: str, limit: int = 50) -> dict:
        """Points from one memory collection, as JSON-safe dicts.
        limit defaults to 50 and is capped at 200. No memory
        configured is an empty list, never an error.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        if self.memory is None:
            return {"points": []}
        try:
            points = self.memory.list_points(collection, limit=limit)
        except Exception as exc:
            return {"points": [], "total": 0,
                    "error": f"{type(exc).__name__}: {exc}"}
        out = []
        for point in points:
            out.append({"id": getattr(point, "id", ""),
                        "text": getattr(point, "text", ""),
                        "payload": dict(getattr(point, "payload", {}) or {})})
        # Keep the compact empty response compatible with older clients;
        # the studio already derives zero from an empty points list.
        if not out:
            return {"points": []}
        return {"points": out, "total": len(out),
                "mode": type(getattr(self.memory, "store", None)).__name__}

    def handle_memory_point_delete(self, collection: str, point_id: str) -> dict:
        """Forget one point, via the same forget path the studio
        memory handler uses. Unknown points (or no memory) report
        ok False, which the route maps to 404.
        """
        if self.memory is None:
            return {"ok": False}
        try:
            return {"ok": bool(self.memory.forget(collection, point_id))}
        except Exception:
            return {"ok": False}

    def make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def _send_json(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    # Browsers and frontend fetches can cancel a request
                    # while a lazy memory/model operation is finishing.
                    # The client disconnect is not a server failure.
                    return

            def _send_html(self, status: int, html: str):
                data = html.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def _send_bytes(self, status: int, content_type: str, data: bytes):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _send_static(self, relative: str):
                """Serve only files from the bundled WebUI assets."""
                candidate = (STATIC_DIR / relative).resolve()
                root = STATIC_DIR.resolve()
                if root not in candidate.parents or not candidate.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                data = candidate.read_bytes()
                if candidate.name in {"index.html", "dag.html", "capabilities.html",
                                      "memory.html", "knowledge.html", "reports.html",
                                      "graph.html", "docs.html"}:
                    # Version the document against its dependent core assets
                    # too.  Otherwise a CSS/JS edit made after index.html
                    # keeps the browser's old cached files indefinitely.
                    asset_times = [candidate.stat().st_mtime]
                    for asset_name in ("style.css", "app.js", "avatar.js"):
                        asset = STATIC_DIR / asset_name
                        if asset.is_file():
                            asset_times.append(asset.stat().st_mtime)
                    version = str(int(max(asset_times))).encode()
                    data = data.replace(b"__ASSET_V__", version)
                data = data.replace(b"__AIDON_VERSION__", _app_version().encode())
                content_type = mimetypes.guess_type(str(candidate))[0] or \
                    "application/octet-stream"
                self._send_bytes(200, content_type, data)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", 0) or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith(("/webhook/", "/webhooks/")):
                    from .webui_auth import is_allowed_host, is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    if not is_allowed_host(self.headers.get("Host", ""), server.webui_allowed_hosts):
                        self._send_json(403, {"error": "foreign host"})
                        return
                if server.webhook_only and parsed.path not in (
                        "/webhook/whatsapp", "/webhooks/whatsapp"):
                    self._send_json(404, {"error": "webhook only"})
                elif parsed.path == "/static" or parsed.path.startswith("/static/"):
                    relative = parsed.path[len("/static/"):] if parsed.path.startswith("/static/") else ""
                    self._send_static(relative)
                elif parsed.path == "/":
                    self._send_static("index.html")
                elif parsed.path in ("/dag", "/capabilities", "/memory", "/knowledge",
                                     "/reports", "/practice", "/graph", "/documents"):
                    self._send_static(parsed.path.lstrip("/") + ".html")
                elif parsed.path == "/settings":
                    keys = server.auth_store.list_keys() if server.auth_store else []
                    html = """<div class="settings-shell">
<div class="settings-top"><a href="/">← Back to WebUI</a><h1>Settings</h1><button id="theme-toggle" title="Toggle dark / light theme">◐</button></div>
<div class="settings-intro">Tune Aidon in one place. Changes are written to <code id="env-path"></code> and take effect after restarting Aidon. Secrets are never displayed; <span class="set-badge">SET</span> means a value already exists.</div>
<div id="settings-groups"></div>
<div id="settings-footer"><span id="settings-note">Use sliders where safe bounds are known; fields without bounds accept a value directly.</span><button id="settings-save">Save settings</button><span id="settings-status"></span></div>
<div class="set-group"><h2>API access keys</h2>"""
                    for key in keys:
                        html += _api_key_row(key)
                    html += """</div></div><script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function loadSettings(){
  const r = await fetch('/api/settings/catalog'); const data = await r.json();
  document.getElementById('env-path').textContent = data.env_file || '';
  let out = '';
  (data.groups || []).forEach(function(g){
    out += '<section class="set-group"><h2>' + esc(g.group) + '</h2>';
    g.settings.forEach(function(s){
      out += '<div class="set-row"><div class="set-label"><label>' + esc(s.label) + '</label>' + (s.is_set ? '<span class="set-badge">SET</span>' : '') + '<span class="set-key">' + esc(s.key) + '</span>';
      if (s.desc) out += '<div class="set-desc">' + esc(s.desc) + '</div>';
      out += '</div><div class="set-input">';
      if (s.type === 'bool') {
        out += '<label class="switch"><input type="checkbox" data-key="' + esc(s.key) + '"' + ((s.value === '1' || s.value === 'true') ? ' checked' : '') + '><span></span></label>';
      } else if (s.type === 'enum') {
        out += '<select data-key="' + esc(s.key) + '">' + (s.options || []).map(function(o){ return '<option' + (o === s.value ? ' selected' : '') + '>' + esc(o) + '</option>'; }).join('') + '</select>';
      } else if (s.type === 'int' && s.min !== undefined && s.max !== undefined) {
        const n = Number(s.value); const safe = Number.isFinite(n) ? Math.min(s.max, Math.max(s.min, n)) : s.min;
        out += '<div class="range-control"><input type="range" min="' + s.min + '" max="' + s.max + '" step="' + (s.step || 1) + '" value="' + safe + '" data-range-for="' + esc(s.key) + '"><input type="number" min="' + s.min + '" max="' + s.max + '" step="' + (s.step || 1) + '" data-key="' + esc(s.key) + '" value="' + esc(s.value) + '"></div><div class="range-meta"><span>' + s.min + '</span><span>' + s.max + '</span></div>';
      } else if (s.type === 'int') {
        out += '<input type="number" data-key="' + esc(s.key) + '" value="' + esc(s.value) + '">';
      } else if (s.secret) {
        out += '<input type="password" data-key="' + esc(s.key) + '" placeholder="' + (s.is_set ? '******** (set - leave empty to keep, type to replace)' : 'not set - type a value to set') + '">';
      } else {
        out += '<input type="text" data-key="' + esc(s.key) + '" value="' + esc(s.value) + '">';
      }
      out += '</div></div>';
    });
    out += '</section>';
  });
  document.getElementById('settings-groups').innerHTML = out;
  document.querySelectorAll('[data-range-for]').forEach(function(range){
    const number = document.querySelector('[data-key="' + range.getAttribute('data-range-for') + '"]');
    if (!number) return;
    range.addEventListener('input', function(){ number.value = range.value; });
    number.addEventListener('input', function(){ if (number.value !== '') range.value = number.value; });
  });
}
async function saveSettings(){
  const status = document.getElementById('settings-status'); status.textContent = 'Saving...';
  const fields = document.querySelectorAll('[data-key]'); let failed = 0;
  for (const f of fields) {
    let v = (f.type === 'checkbox') ? (f.checked ? '1' : '0') : f.value;
    if (f.type === 'password' && (!v || v === '****')) { continue; }
    const r = await fetch('/api/settings/env', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: f.getAttribute('data-key'), value: v})});
    const d = await r.json();
    if (!d.ok) { failed++; status.textContent = 'Failed: ' + (d.error || 'unknown'); }
  }
  if (!failed) { status.textContent = 'Saved. Restart Aidon for changes to take effect.'; }
}
document.getElementById('settings-save').addEventListener('click', saveSettings);
document.getElementById('theme-toggle').addEventListener('click', function(){
  const light = document.body.classList.toggle('light'); localStorage.setItem('aidon-theme', light ? 'light' : 'dark');
});
loadSettings();
</script>"""
                    self._send_html(200, _page_html("Settings", html))
                elif parsed.path == "/library":
                    files = server._library_files()
                    html = "<h1>Library</h1>"
                    for item in files:
                        html += f"<p>{item['path']} {item['size']} bytes</p>"
                    self._send_html(200, _page_html("Library", html))
                elif parsed.path.startswith("/api/library/") and parsed.path.endswith("/file"):
                    from ..memory.library import Library
                    item_id = parsed.path[len("/api/library/"):-len("/file")].strip("/")
                    item = Library(server.home).get_item(item_id)
                    if item is None:
                        self._send_json(404, {"error": "library item not found"})
                    else:
                        data = Path(item["absolute_path"]).read_bytes()
                        content_type = item.get("mime") or "application/octet-stream"
                        self._send_bytes(200, content_type, data)
                elif parsed.path == "/api/library":
                    from ..memory.library import Library
                    query = parse_qs(parsed.query)
                    items = Library(server.home).list_items()
                    # Keep the legacy file listing visible for homes created
                    # before the library manifest was introduced.
                    if not items:
                        items = server._library_files()
                    self._send_json(200, {"files": items})
                elif parsed.path == "/api/approvals":
                    # Auth already checked at the top of do_GET,
                    # the same is_authorized check every /api/
                    # route gets. Only still-pending cards: a
                    # decided or timed-out card is gone here.
                    self._send_json(200, {"cards": server.webui_gate.pending_cards()})
                elif parsed.path == "/api/preapprovals":
                    self._send_json(200, server.handle_preapprovals())
                elif parsed.path == "/factory":
                    self._send_html(200, FACTORY_HTML)
                elif parsed.path == "/api/factory":
                    self._send_json(200, {"stages": server.factory_stages()})
                elif parsed.path in ("/dag", "/studio/dag"):
                    self._send_html(200, DAG_HTML)
                elif parsed.path == "/api/dags":
                    self._send_json(200, server.handle_dags_list())
                elif parsed.path.startswith("/api/dags/"):
                    dag_id = parsed.path[len("/api/dags/"):]
                    data = server.handle_dag_get(dag_id)
                    self._send_json(200 if "error" not in data else 404, data)
                elif parsed.path in ("/capabilities", "/studio/capabilities"):
                    from ..capabilities.plugins import PluginRegistry as _PR
                    _reg = _PR(server.home); _reg.load_all()
                    html = "<h1>Capabilities</h1><p>What Aidon can do, what it has learned, and the evidence.</p>"
                    html += f"<p>Sandbox tools: write, read, list, shell, calculate</p><p>Plugin tools: {', '.join(_reg.tool_names())}</p>"
                    html += "<p>Computer use: screenshot and click, type, scroll, key, approval-gated, no driver configured by default, no GUI action without a driver.</p>"
                    html += "<h2>Capabilities studio</h2><p>Record a skill success or failure, or probe a plugin dir, or edit a skill's description, via POST /api/studio/capabilities with JSON {action: success|failure|probe|edit, name, path, description}.</p>"
                    html += "<form onsubmit=\"event.preventDefault(); fetch('/api/studio/capabilities',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:document.getElementById('cap_action').value,name:document.getElementById('cap_name').value,path:document.getElementById('cap_path').value,description:document.getElementById('cap_desc').value})}).then(r=>r.json()).then(d=>document.getElementById('cap_result').textContent=JSON.stringify(d));\"><select id=\"cap_action\"><option>success</option><option>failure</option><option>probe</option></select> <input id=\"cap_name\" placeholder=\"skill name\"> <input id=\"cap_path\" placeholder=\"plugin dir for probe\"> <button>Run</button></form><pre id=\"cap_result\"></pre>"
                    self._send_html(200, _page_html("Capabilities", html))
                elif parsed.path in ("/memory", "/studio/memory"):
                    html = "<h1>Memory</h1><p>Episodes Aidon remembers from past work, by collection, no secret values.</p>"
                    if server.memory is not None:
                        for coll in ("working", "episodic", "semantic", "experience", "knowledge"):
                            try:
                                points = server.memory.list_points(coll, limit=20)
                                html += f"<h2>{coll}: {len(points)} points</h2>"
                                for point in points:
                                    html += f"<p>{point.id} {point.payload.get('text', '')[:120]}</p>"
                            except Exception:
                                continue
                    html += "<h2>Memory studio</h2><p>Forget a point, prune a collection, or promote an episodic point, or edit a point's text, via POST /api/studio/memory with JSON {action: forget|prune|promote|edit, collection, point_id, text}.</p>"
                    html += "<form onsubmit=\"event.preventDefault(); fetch('/api/studio/memory',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:document.getElementById('mem_action').value,collection:document.getElementById('mem_coll').value,point_id:document.getElementById('mem_id').value,text:document.getElementById('mem_text').value})}).then(r=>r.json()).then(d=>document.getElementById('mem_result').textContent=JSON.stringify(d));\"><select id=\"mem_action\"><option>forget</option><option>prune</option><option>promote</option></select> <input id=\"mem_coll\" value=\"episodic\"> <input id=\"mem_id\" placeholder=\"point id\"> <button>Run</button></form><pre id=\"mem_result\"></pre>"
                    self._send_html(200, _page_html("Memory", html))
                elif parsed.path in ("/knowledge", "/studio/knowledge"):
                    html = "<h1>Knowledge</h1><p>Documents and facts Aidon has ingested, from the knowledge collection and library.</p>"
                    if server.memory is not None:
                        try:
                            for point in server.memory.list_points("knowledge", limit=50):
                                html += f"<p>{point.id} {point.payload.get('text', '')[:120]}</p>"
                        except Exception:
                            pass
                    for item in server._library_files():
                        html += f"<p>Library: {item['path']}</p>"
                    html += "<h2>Knowledge studio</h2><p>Recall, record, edit, or delete knowledge, via POST /api/studio/knowledge with JSON {action: recall|ingest|edit|delete, query, text, point_id}.</p>"
                    html += "<form onsubmit=\"event.preventDefault(); fetch('/api/studio/knowledge',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:document.getElementById('kb_action').value,query:document.getElementById('kb_query').value,text:document.getElementById('kb_text').value})}).then(r=>r.json()).then(d=>document.getElementById('kb_result').textContent=JSON.stringify(d));\"><select id=\"kb_action\"><option>recall</option><option>ingest</option></select> <input id=\"kb_query\" placeholder=\"query\"> <input id=\"kb_text\" placeholder=\"text to ingest\"> <button>Run</button></form><pre id=\"kb_result\"></pre>"
                    self._send_html(200, _page_html("Knowledge", html))
                elif parsed.path in ("/reports", "/studio/reports"):
                    from ..interface.scheduler_loop import daily_report as _daily
                    html = "<h1>Reports</h1><p>Daily reports, from learned last run and trace files, never invented.</p>"
                    html += f"<pre>{_daily(server.home)}</pre>"
                    self._send_html(200, _page_html("Reports", html))
                elif parsed.path == "/replay":
                    self._send_html(200, REPLAY_HTML)
                elif parsed.path == "/avatar-vrm":
                    self._send_html(200, VRM_HTML)
                elif parsed.path == "/api/replay":
                    query = parse_qs(parsed.query)
                    run = query.get("run", [""])[0]
                    if run:
                        self._send_json(200, {"run": run, "events": server.replay_events(run)})
                    else:
                        self._send_json(200, {"runs": server.replay_runs()})
                elif parsed.path == "/api/settings/catalog":
                    self._send_json(200, server.settings_catalog())
                elif parsed.path == "/api/keys":
                    self._send_json(200, server.handle_keys())
                elif parsed.path == "/api/settings":
                    self._send_json(200, server.settings_get())
                elif parsed.path == "/api/avatar.vrm":
                    vrm_path = server._avatar_path()
                    if vrm_path is None:
                        self._send_json(404, {"error": "no VRM file"})
                    else:
                        data = vrm_path.read_bytes()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                elif parsed.path == "/api/memory-dags":
                    dags = []
                    if server.memory is not None:
                        try:
                            from ..memory.experience import recall_experiences
                            for exp in recall_experiences(server.memory, "", limit=20):
                                if exp.dag_nodes:
                                    dags.append({"goal": exp.goal, "nodes": exp.dag_nodes})
                        except Exception:
                            pass
                    self._send_json(200, {"dags": dags})
                elif parsed.path == "/api/entities":
                    query = parse_qs(parsed.query)
                    entity = query.get("entity", [""])[0]
                    self._send_json(200, server.handle_entities(entity))
                elif parsed.path == "/api/memory/graph":
                    query = parse_qs(parsed.query)
                    collections = query.get("collection", []) or None
                    try:
                        limit = int(query.get("limit", ["500"])[0])
                    except ValueError:
                        limit = 500
                    self._send_json(200, server.handle_memory_graph(collections, limit))
                elif parsed.path == "/api/tools":
                    self._send_json(200, server.handle_tools())
                elif parsed.path.startswith("/api/runs/") and "/" not in parsed.path[len("/api/runs/"):]:
                    run_id = parsed.path[len("/api/runs/"):]
                    query = parse_qs(parsed.query)
                    try:
                        since = max(0, int(query.get("since", ["0"])[0]))
                    except (TypeError, ValueError):
                        since = 0
                    try:
                        trace_since = max(0, int(query.get("trace_since", ["0"])[0]))
                    except (TypeError, ValueError):
                        trace_since = 0
                    data = server.run_snapshot(run_id, since=since,
                                               trace_since=trace_since)
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "run not found"})
                elif parsed.path == "/api/think":
                    from .surfaces import think_enabled as _get_think
                    from urllib.parse import parse_qs as _parse_qs_think
                    _think_query = _parse_qs_think(parsed.query)
                    conv = str(_think_query.get("conversation_id", [""])[0])
                    self._send_json(200, {"think_max": _get_think(server.home, conv)})
                elif parsed.path == "/api/research":
                    from .surfaces import research_enabled as _get_research
                    from urllib.parse import parse_qs as _parse_qs_research
                    _research_query = _parse_qs_research(parsed.query)
                    conv = str(_research_query.get("conversation_id", [""])[0])
                    self._send_json(200, {"research_mode": _get_research(server.home, conv)})
                elif parsed.path == "/api/practice":
                    self._send_json(200, server.handle_practice_list())
                elif parsed.path == "/api/conversations":
                    self._send_json(200, server.handle_conversations_list())
                elif parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/brief"):
                    conversation_id = parsed.path[len("/api/conversations/"):-len("/brief")].strip("/")
                    data = server.handle_brief_get(conversation_id)
                    self._send_json(200 if data is not None else 404, data or {"error": "not found"})
                elif parsed.path.startswith("/api/conversations/"):
                    conv_id = parsed.path[len("/api/conversations/"):]
                    data = server.handle_conversation_get(conv_id)
                    self._send_json(200 if data is not None else 404, data or {"error": "not found"})
                elif parsed.path == "/api/mode-stops/latest":
                    query = parse_qs(parsed.query)
                    conv = query.get("conversation_id", [""])[0]
                    data = server.handle_mode_stop_latest(conv)
                    self._send_json(200 if data is not None else 404, data or {"error": "not found"})
                elif parsed.path == "/api/ideas":
                    self._send_json(200, server.handle_ideas_list())
                elif parsed.path.startswith("/api/files/"):
                    from urllib.parse import unquote
                    requested = unquote(parsed.path[len("/api/files/"):])
                    status, content_type, data = server.handle_file(requested)
                    self._send_bytes(status, content_type, data)
                elif parsed.path == "/api/files":
                    # Same-origin file serving: the auth check at the
                    # top of do_GET already ran (is_authorized), so
                    # this branch only confines the path to home.
                    query = parse_qs(parsed.query)
                    requested = query.get("path", [""])[0]
                    status, content_type, data = server.handle_file(requested)
                    self._send_bytes(status, content_type, data)
                elif parsed.path == "/api/voice":
                    self._send_json(200, server.handle_voice())
                elif parsed.path == "/api/status":
                    self._send_json(200, server.handle_status())
                elif parsed.path == "/api/health":
                    runs = server.run_control.list_runs() if server.run_control else []
                    _status = server.handle_status()
                    self._send_json(200, {"ok": True, "runs": len(runs),
                                          "memory": _status.get("memory"),
                                          "memory_warning": _status.get("memory_warning", "")})
                elif parsed.path == "/api/avatar/status":
                    vrm_path = server._avatar_path()
                    self._send_json(200, {"present": vrm_path is not None,
                                          "source": str(vrm_path) if vrm_path else None,
                                          "default": "robot",
                                          "active": "robot"})
                elif parsed.path == "/api/reports":
                    self._send_json(200, server.handle_reports_list())
                elif parsed.path.startswith("/api/reports/"):
                    day = parsed.path[len("/api/reports/"):].strip("/")
                    data = server.handle_report_get(day)
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "not found"})
                elif parsed.path == "/api/missions":
                    self._send_json(200, server.handle_missions_list())
                elif parsed.path.startswith("/api/missions/"):
                    mission_id = parsed.path[len("/api/missions/"):].strip("/")
                    data = server.handle_mission_get(mission_id)
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "mission not found"})
                elif parsed.path == "/api/capabilities":
                    self._send_json(200, server.handle_capabilities())
                elif parsed.path == "/api/capabilities/code":
                    query = parse_qs(parsed.query)
                    data = server.handle_capability_code(query.get("name", [""])[0])
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "capability not found"})
                elif parsed.path in ("/api/memory/episodes", "/api/memory/knowledge"):
                    query = parse_qs(parsed.query)
                    collection = parsed.path.rsplit("/", 1)[-1]
                    try:
                        limit = int(query.get("limit", ["50"])[0])
                    except ValueError:
                        limit = 50
                    try:
                        offset = max(0, int(query.get("offset", ["0"])[0]))
                    except ValueError:
                        offset = 0
                    needle = query.get("q", [""])[0].strip().lower()
                    raw_points = server.handle_memory_points(collection, 200).get("points", [])
                    items = []
                    for point in raw_points:
                        item = dict(point.get("payload", {}) or {})
                        item["id"] = point.get("id", "")
                        item.setdefault("text", point.get("text", ""))
                        items.append(item)
                    if needle:
                        items = [item for item in items if needle in json.dumps(item).lower()]
                    matched = len(items)
                    page = items[offset:offset + max(1, min(limit, 200))]
                    # "items" is what the memory page renders; "points"
                    # is the original key this route answered with, kept
                    # so older clients (and the route tests) keep working.
                    self._send_json(200, {"items": page, "points": page,
                                          "total": len(raw_points),
                                          "matched": matched, "mode": "Aidon memory"})
                elif parsed.path in ("/webhook/whatsapp", "/webhooks/whatsapp"):
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
                if not parsed.path.startswith(("/webhook/", "/webhooks/")):
                    from .webui_auth import is_allowed_host, is_allowed_origin, is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    if not is_allowed_host(self.headers.get("Host", ""), server.webui_allowed_hosts):
                        self._send_json(403, {"error": "foreign host"})
                        return
                    if not is_allowed_origin(headers, self.headers.get("Host", "")):
                        self._send_json(403, {"error": "foreign origin"})
                        return
                if server.webhook_only and parsed.path not in (
                        "/webhook/whatsapp", "/webhooks/whatsapp",
                        "/webhook/slack", "/webhooks/slack"):
                    self._send_json(404, {"error": "webhook only"})
                    return
                raw = self._read_body()
                content_type = self.headers.get("Content-Type", "").lower()
                if content_type.startswith("audio/"):
                    # Audio uploads are binary and must reach /api/transcribe
                    # unchanged; attempting json.loads here raises a
                    # UnicodeDecodeError before that route can handle them.
                    body = {}
                else:
                    try:
                        body = json.loads(raw) if raw else {}
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        body = {}
                if parsed.path == "/api/goals/with-images" and content_type.startswith("multipart/form-data"):
                    try:
                        fields, files = server.parse_goal_multipart(raw, content_type)
                        result = server.handle_goal_with_files(fields, files)
                        self._send_json(200 if "run_id" in result else 400, result)
                    except (ValueError, OSError) as exc:
                        self._send_json(400, {"error": str(exc)})
                    return
                if parsed.path == "/api/library/upload":
                    import base64 as _b64
                    name = str(body.get("name", "upload.bin"))
                    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_", ".")) or "upload.bin"
                    try:
                        data = _b64.b64decode(str(body.get("content_b64", "")))
                    except Exception:
                        self._send_json(400, {"error": "bad base64"})
                        return
                    tmp = server.home / "library" / "uploads" / safe
                    tmp.parent.mkdir(parents=True, exist_ok=True)
                    tmp.write_bytes(data)
                    from ..memory.library import Library
                    saved = Library(server.home).add_file(tmp, kind="file", description="WebUI upload")
                    self._send_json(200, {"id": saved["id"], "name": safe})
                elif parsed.path == "/api/settings":
                    if isinstance(body.get("settings"), dict):
                        updated = []
                        errors = []
                        for key, value in body["settings"].items():
                            result = server.settings_set_env(str(key), value)
                            if result.get("ok"):
                                updated.append(str(key))
                            else:
                                errors.append(result.get("error", str(key)))
                        self._send_json(200 if not errors else 400,
                                        {"updated": updated, "errors": errors})
                    else:
                        key = str(body.get("key", ""))
                        if server.settings_set(key, body.get("value")):
                            self._send_json(200, {"ok": True})
                        else:
                            result = server.settings_set_env(key, body.get("value", ""))
                            self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path == "/api/settings/env":
                    result = server.settings_set_env(str(body.get("key", "")), body.get("value", ""))
                    self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path == "/api/think":
                    from .surfaces import toggle_think as _post_think
                    conv = str(body.get("conversation_id", ""))
                    state = _post_think(server.home, conv)
                    self._send_json(200, {"think_max": state})
                elif parsed.path == "/api/research":
                    from .surfaces import toggle_research as _post_research
                    conv = str(body.get("conversation_id", ""))
                    state = _post_research(server.home, conv)
                    self._send_json(200, {"research_mode": state})
                elif parsed.path == "/api/transcribe_stream/start":
                    if not server.transcribe_key:
                        self._send_json(501, {"error": "transcribe key not configured"})
                        return
                    try:
                        import uuid as _uuidt
                        from ..capabilities.voice import RealtimeTranscriber as _RT
                        sid = _uuidt.uuid4().hex
                        rt = _RT(server.transcribe_key, session_id=sid)
                        rt.connect()
                        server.realtime_sessions[sid] = rt
                        self._send_json(200, {"session_id": sid})
                    except Exception as exc:
                        self._send_json(502, {"error": str(exc)})
                elif parsed.path == "/api/transcribe_stream/audio":
                    import base64 as _b64a
                    rt = server.realtime_sessions.get(str(body.get("session_id", "")))
                    if rt is None:
                        self._send_json(404, {"error": "unknown session"})
                        return
                    try:
                        audio = _b64a.b64decode(str(body.get("audio_b64", "")))
                    except Exception:
                        self._send_json(400, {"error": "bad base64"})
                        return
                    if audio and not rt.send_audio(audio):
                        detail = rt.error or ""
                        self._send_json(410, {"error": "session closed" + (": " + detail if detail else "")})
                        return
                    self._send_json(200, {"transcript": rt.transcript,
                                          "final": rt.final})
                elif parsed.path == "/api/transcribe_stream/end":
                    rt = server.realtime_sessions.pop(str(body.get("session_id", "")), None)
                    if rt is None:
                        self._send_json(404, {"error": "unknown session"})
                        return
                    try:
                        result = rt.end()
                    finally:
                        rt.close()
                    self._send_json(200, result)
                elif parsed.path == "/api/transcribe":
                    if not server.transcribe_key:
                        self._send_json(501, {"error": "transcribe key not configured"})
                        return
                    import base64 as _b64t
                    try:
                        # The browser sends the MediaRecorder blob directly,
                        # matching the compatibility endpoint. Keep JSON/base64 as
                        # a compatibility form for API clients.
                        if raw and self.headers.get("Content-Type", "").lower().startswith("audio/"):
                            audio = raw
                        else:
                            audio = _b64t.b64decode(str(body.get("audio_b64", "")))
                        from ..capabilities.voice import transcribe as _transcribe
                        content_type = self.headers.get("Content-Type", "audio/wav")
                        result = _transcribe(audio, server.transcribe_key,
                                             content_type=content_type)
                        self._send_json(200, result)
                    except Exception as exc:
                        self._send_json(500, {"error": str(exc)})
                elif parsed.path == "/api/goals":
                    result = server.start_goal(body)
                    self._send_json(200 if "run_id" in result else 400, result)
                elif parsed.path.startswith("/api/mode-stops/") and parsed.path.endswith("/spec"):
                    stop_id = parsed.path[len("/api/mode-stops/"):-len("/spec")].strip("/")
                    result = server.handle_mode_stop_spec_save(stop_id, body)
                    self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path == "/api/proceed":
                    # The Proceed button on a stopped run: reuse the
                    # typed /proceed path, so the button and the
                    # command dispatch the same resume run.
                    stop_id = str(body.get("stop_id", "") or "")
                    result = server.start_goal({
                        "goal": f"/proceed {stop_id}".strip(),
                        "conversation_id": str(body.get("conversation_id", "") or ""),
                    })
                    self._send_json(200 if "run_id" in result else 400, result)
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/approval"):
                    run_id = parsed.path[len("/api/runs/"):-len("/approval")].strip("/")
                    pending = server.webui_gate.pending_cards()
                    if not pending:
                        self._send_json(409, {"error": "no pending approval"})
                    else:
                        decision = str(body.get("decision", body.get("verdict", "rejected")))
                        ok = server.webui_gate.approve(
                            pending[0]["id"], decision,
                            str(body.get("edited_text", "")))
                        self._send_json(200 if ok else 409, {"ok": ok, "run_id": run_id})
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/steer"):
                    run_id = parsed.path[len("/api/runs/"):-len("/steer")].strip("/")
                    ok = server.run_control.steer(run_id, str(body.get("note", ""))) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                    run_id = parsed.path[len("/api/runs/"):-len("/cancel")].strip("/")
                    ok = server.run_control.cancel(run_id, gate=server.webui_gate) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel-queued"):
                    run_id = parsed.path[len("/api/runs/"):-len("/cancel-queued")].strip("/")
                    record = server.run_control.get(run_id) if server.run_control else None
                    if record is None:
                        self._send_json(404, {"error": "run not found"})
                    else:
                        self._send_json(409, {"error": "run is not queued"})
                elif parsed.path == "/api/chat":
                    self._send_json(200, server.handle_chat(body))
                elif parsed.path == "/api/dags":
                    status, payload = server.handle_dag_save(body)
                    self._send_json(status, payload)
                elif parsed.path == "/api/dags/layout":
                    status, payload = server.handle_dag_layout(body)
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
                    ok = server.webui_gate.approve(
                        card_id, verdict, str(body.get("edited_text", "")))
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path == "/api/preapprovals":
                    status, payload = server.handle_preapproval_grant(body)
                    self._send_json(status, payload)
                elif parsed.path == "/api/studio/memory":
                    if str(body.get("action", "")) == "edit":
                        # Edit works with or without a live memory
                        # store: without one it is recorded, never
                        # dropped, under <home>/memory_edits/.
                        result = server.handle_memory_edit(
                            str(body.get("collection", "episodic")),
                            str(body.get("point_id", "")),
                            str(body.get("text", "")))
                        self._send_json(200 if result.get("ok") else 400, result)
                    elif server.memory is None:
                        self._send_json(500, {"error": "no memory"})
                    else:
                        action = str(body.get("action", ""))
                        collection = str(body.get("collection", "episodic"))
                        point_id = str(body.get("point_id", ""))
                        if action == "forget":
                            self._send_json(200, {"ok": server.memory.forget(collection, point_id)})
                        elif action == "prune":
                            self._send_json(200, server.memory.prune(collection))
                        elif action == "promote":
                            point = server.memory.store.get(collection, point_id)
                            if point is None:
                                self._send_json(404, {"error": "point not found"})
                            else:
                                self._send_json(200, {"ok": True, "result": str(server.memory.promote_to_ltm(point))})
                        else:
                            self._send_json(400, {"error": "unknown memory studio action"})
                elif parsed.path == "/api/studio/knowledge":
                    if server.memory is None:
                        self._send_json(500, {"error": "no memory"})
                    else:
                        action = str(body.get("action", ""))
                        if action == "recall":
                            self._send_json(200, {"results": [str(r) for r in server.memory.recall("knowledge", str(body.get("query", "")))]})
                        elif action == "ingest":
                            server.memory.record("knowledge", str(body.get("text", "")))
                            self._send_json(200, {"ok": True})
                        elif action == "delete":
                            point_id = str(body.get("point_id", ""))
                            self._send_json(200, {"ok": server.memory.forget("knowledge", point_id)})
                        elif action == "edit":
                            point_id = str(body.get("point_id", ""))
                            text = str(body.get("text", ""))
                            if not point_id or not text:
                                self._send_json(400, {"error": "edit needs point_id and text"})
                            else:
                                ok = server.memory.forget("knowledge", point_id)
                                server.memory.record("knowledge", text)
                                self._send_json(200, {"ok": ok, "edited": True})
                        else:
                            self._send_json(400, {"error": "unknown knowledge studio action"})
                elif parsed.path == "/api/studio/capabilities":
                    action = str(body.get("action", ""))
                    if action == "edit":
                        result = server.handle_skill_edit(
                            str(body.get("name", "")), str(body.get("description", "")))
                        self._send_json(200 if result.get("ok") else 400, result)
                    elif action in ("success", "failure"):
                        from ..capabilities.skill_lifecycle import LifecycleStore
                        lifecycle = LifecycleStore(server.home)
                        name = str(body.get("name", ""))
                        result = lifecycle.record_success(name) if action == "success" else lifecycle.record_failure(name)
                        self._send_json(200, {"ok": True, "state": result})
                    elif action == "probe":
                        from ..capabilities.security_probe import probe_plugin
                        path = str(body.get("path", ""))
                        if not path:
                            self._send_json(400, {"error": "probe needs a path"})
                        else:
                            self._send_json(200, {"findings": probe_plugin(path)})
                    else:
                        self._send_json(400, {"error": "unknown capabilities studio action"})
                elif parsed.path == "/api/conversations":
                    self._send_json(200, server.handle_conversation_create(body))
                elif parsed.path == "/api/ideas":
                    self._send_json(200, server.handle_idea_create(body))
                elif parsed.path == "/api/hub/runs":
                    result = server.handle_hub_run_create(body)
                    self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path.startswith("/api/hub/runs/") and parsed.path.endswith("/events"):
                    run_id = parsed.path[len("/api/hub/runs/"):-len("/events")].strip("/")
                    self._send_json(200, server.handle_hub_run_event(run_id, body))
                elif parsed.path.startswith("/api/hub/runs/") and parsed.path.endswith("/stage"):
                    run_id = parsed.path[len("/api/hub/runs/"):-len("/stage")].strip("/")
                    self._send_json(200, server.handle_hub_run_stage(run_id, body))
                elif parsed.path.startswith("/api/hub/runs/") and parsed.path.endswith("/trace"):
                    run_id = parsed.path[len("/api/hub/runs/"):-len("/trace")].strip("/")
                    self._send_json(200, server.handle_hub_run_trace(run_id, body))
                elif parsed.path.startswith("/api/hub/runs/") and parsed.path.endswith("/finish"):
                    run_id = parsed.path[len("/api/hub/runs/"):-len("/finish")].strip("/")
                    result = server.finish_hub_run(run_id)
                    self._send_json(200 if result.get("ok") else 404, result)
                elif parsed.path == "/api/reports/generate":
                    self._send_json(200, server.handle_report_generate(body))
                elif parsed.path == "/api/tts":
                    status, payload = server.handle_tts(body)
                    audio = payload.pop("_audio", None) if isinstance(payload, dict) else None
                    if status == 200 and isinstance(audio, (bytes, bytearray)):
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/mpeg")
                        self.send_header("Content-Length", str(len(audio)))
                        self.end_headers()
                        try:
                            self.wfile.write(audio)
                        except (BrokenPipeError, ConnectionResetError):
                            return
                    else:
                        self._send_json(status, payload)
                elif parsed.path == "/api/tts/stream":
                    try:
                        response = server.open_tts_stream(body)
                    except Exception as exc:
                        self._send_json(500, {"error": type(exc).__name__})
                    else:
                        self.send_response(200)
                        self.send_header("Content-Type", "audio/mpeg")
                        self.send_header("Cache-Control", "no-store")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        try:
                            while True:
                                chunk = response.read(4 * 1024)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        finally:
                            response.close()
                elif parsed.path == "/api/goals/with-images":
                    self._send_json(200, server.handle_goal_with_images(body))
                elif parsed.path == "/api/plugins/adopt":
                    result = server.handle_plugin_adopt(str(body.get("path", "")))
                    self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path == "/api/practice":
                    result = server.handle_practice_create(body)
                    self._send_json(200 if "schedule" in result else 400, result)
                elif parsed.path == "/api/practice/delete":
                    self._send_json(200, server.handle_practice_delete(body))
                elif parsed.path == "/api/practice/run":
                    result = server.handle_practice_run(body)
                    self._send_json(200 if result.get("started") else 400, result)
                elif parsed.path in ("/webhook/whatsapp", "/webhooks/whatsapp"):
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_whatsapp_post(body, raw, headers)
                    self._send_json(status, payload)
                elif parsed.path in ("/webhook/slack", "/webhooks/slack"):
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_slack_post(raw, headers, body)
                    # Slack Events API URL verification expects the
                    # challenge in a JSON response body.
                    self._send_json(status, payload)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_DELETE(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith(("/webhook/", "/webhooks/")):
                    from .webui_auth import is_allowed_host, is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    if not is_allowed_host(self.headers.get("Host", ""), server.webui_allowed_hosts):
                        self._send_json(403, {"error": "foreign host"})
                        return
                if parsed.path.startswith("/api/memory/episodes/") or \
                        parsed.path.startswith("/api/memory/knowledge/"):
                    parts = parsed.path.split("/")
                    collection = parts[3]
                    point_id = "/".join(parts[4:]).strip("/")
                    result = server.handle_memory_point_delete(collection, point_id)
                    self._send_json(200 if result["ok"] else 404, result)
                elif parsed.path.startswith("/api/preapprovals/"):
                    rest = parsed.path[len("/api/preapprovals/"):].strip("/")
                    parts = rest.split("/")
                    spec_id = parts[0] if parts else ""
                    kind = parts[1] if len(parts) > 1 else ""
                    status, payload = server.handle_preapproval_revoke(spec_id, kind)
                    self._send_json(status, payload)
                elif parsed.path.startswith("/api/dags/"):
                    dag_id = parsed.path[len("/api/dags/"):]
                    self._send_json(200, server.handle_dag_delete(dag_id))
                elif parsed.path.startswith("/api/conversations/"):
                    conv_id = parsed.path[len("/api/conversations/"):]
                    result = server.handle_conversation_delete(conv_id)
                    self._send_json(200 if result["ok"] else 404, result)
                elif parsed.path.startswith("/api/ideas/"):
                    idea_id = parsed.path[len("/api/ideas/"):]
                    result = server.handle_idea_delete(idea_id)
                    self._send_json(200 if result["ok"] else 404, result)
                elif parsed.path.startswith("/api/mode-stops/"):
                    stop_id = parsed.path[len("/api/mode-stops/"):].strip("/")
                    result = server.handle_mode_stop_delete(stop_id)
                    self._send_json(200 if result["ok"] else 404, result)
                elif parsed.path.startswith("/api/library/"):
                    item_id = parsed.path[len("/api/library/"):].strip("/")
                    from ..memory.library import Library
                    ok = Library(server.home).delete_item(item_id)
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path.startswith("/api/missions/"):
                    mission_id = parsed.path[len("/api/missions/"):].strip("/")
                    result = server.handle_mission_delete(mission_id)
                    self._send_json(200 if result["ok"] else 404, result)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_PUT(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith(("/webhook/", "/webhooks/")):
                    from .webui_auth import is_allowed_host, is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                    if not is_allowed_host(self.headers.get("Host", ""), server.webui_allowed_hosts):
                        self._send_json(403, {"error": "foreign host"})
                        return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                if parsed.path.startswith("/api/conversations/") and parsed.path.endswith("/brief"):
                    conversation_id = parsed.path[len("/api/conversations/"):-len("/brief")].strip("/")
                    self._send_json(200, server.handle_brief_save(conversation_id, body))
                else:
                    self._send_json(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        return Handler

    def serve(self, port: int = 8002) -> None:
        # Threading, so a goal run waiting on an approval card
        # does not block the /api/approve request that resolves it.
        ThreadingHTTPServer(("127.0.0.1", port), self.make_handler()).serve_forever()
