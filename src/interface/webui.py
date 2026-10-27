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
import mimetypes
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .approval import ApprovalGate, Decision
from .messaging import (MessagingConfig, normalize_number, parse_decision,
                        verify_whatsapp_signature)
from .triggers import TriggerDispatcher, TriggerEvent

STATIC_DIR = Path(__file__).parent / "static"

# Shared page style: every WebUI page gets the same base look. The chat
# page previously shipped only avatar-animation CSS and the handler-built
# pages shipped no <head> at all, so they rendered as plain white text.
# No external assets: everything here is inline, so pages work offline.
BASE_CSS = """
:root{--bg:#070b16;--surface:#0d1424;--surface2:#141d33;--text:#e9eef8;--muted:#8b96b3;--border:#1e2a45;--accent:#0082fb;--accent2:#7c5cff;--green:#3ecf8e;--amber:#f5a623;--red:#f0564f}
body.light{--bg:#eef1f7;--surface:#ffffff;--surface2:#e6ebf4;--text:#131a2c;--muted:#5f6b87;--border:#d9e0ee;--accent:#0064e0;--accent2:#6a4dff;--green:#1f9d63;--amber:#c47b12;--red:#d43d36}
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
@media (max-width:700px){#layout{display:block}#sidecol{width:auto;margin-bottom:20px}}
"""


def _page_html(title, body):
    """Wrap a handler-built fragment in a full styled page."""
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + title +
            "</title><style>" + BASE_CSS + "</style></head><body><script>try{if(localStorage.getItem('aidon-theme')==='light'){document.body.classList.add('light')}}catch(e){}</script>" + body + "</body></html>")


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
<div id="avatar" aria-label="Aidon avatar, robot, default, VRM is the fallback">
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
<h2>Projects</h2><div id="projects"></div>
<h2>Library</h2><a href="/library">Library tab</a>
<h2>Settings</h2><a href="/settings">Settings and keys</a>
<h2>Workflows</h2><a href="/dag">DAG editor</a> <a href="/factory">Factory</a> <a href="/replay">Replay</a> <a href="/avatar-vrm">VRM avatar, fallback</a> <a href="/capabilities">Capabilities</a> <a href="/memory">Memory</a> <a href="/knowledge">Knowledge</a> <a href="/reports">Reports</a>
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
  if (reply.includes('Done:')) { setAvatarStatus('Done'); }
  else if (reply.includes('Failed:')) { setAvatarStatus('Failed'); }
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
setInterval(refreshPills, 2000); refreshPills();
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
<h1>VRM avatar, fallback</h1>
<p>Three-dimensional VRM avatar, if a VRM file is present at &lt;home&gt;/avatar.vrm, otherwise the robot SVG avatar in the chat is the default, and this page says so, it does not invent a model. Three.js and three-vrm are loaded from CDN, a VRM file is not bundled, because a model file is the owner's asset, not arena code. Mic here uses Meta Transcribe via /api/transcribe when a transcribe key is configured, otherwise browser SpeechRecognition, as in the chat.</p>
<div id="vrm-view" style="width:600px;height:400px;border:1px solid #ccc"></div>
<p id="vrm-status"></p>
<p><button id="vrm-mic">Transcribe mic</button> <span id="vrm-mic-status"></span></p>
<script>
async function checkVrm() {
  const r = await fetch('/api/avatar.vrm');
  if (r.status === 404) { document.getElementById('vrm-status').textContent = 'No VRM file at <home>/avatar.vrm, using robot SVG avatar in chat as default.'; return; }
  document.getElementById('vrm-status').textContent = 'VRM file found, loading with three-vrm. If the VRM loader fails to load from CDN, the robot SVG remains the default.';
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

    Autonomy and nudges, wired here, not as a separate flow:
    - On card creation, a pending time is recorded, and a
      daemon thread nudges via the Slack sender after
      AIDON_APPROVAL_NUDGE_MIN minutes, once per card, via
      NudgeTracker, so an away owner is nudged, not looped.
    - On approval, AutonomyTracker records the stake, so
      low and medium stakes can earn standing permission
      after the threshold, while high stakes, including
      live execution, never auto-approve. A card that
      qualifies for autonomy returns approved without a card.
    """

    def __init__(self, timeout_seconds: int = 180, nudge_tracker=None,
                 autonomy_tracker=None, slack_sender=None):
        self.timeout_seconds = timeout_seconds
        self.nudge_tracker = nudge_tracker
        self.autonomy_tracker = autonomy_tracker
        self.slack_sender = slack_sender
        self.cards: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._counter = 0
        self.gate = ApprovalGate(self._decide)

    def _decide(self, kind: str, title: str, body: str) -> Decision:
        import time as _time
        stake = "high" if kind == "live" else "medium"
        if self.autonomy_tracker is not None:
            try:
                if self.autonomy_tracker.should_auto_approve(stake, _time.time()):
                    return Decision("approved")
            except Exception:
                pass
        self._counter += 1
        card_id = f"card-{self._counter}"
        event = threading.Event()
        self._events[card_id] = event
        self.cards[card_id] = {"kind": kind, "title": title, "body": body, "verdict": None}
        pending_since = _time.time()
        if self.nudge_tracker is not None:
            def _nudge():
                _time.sleep(max(0, self.nudge_tracker.nudge_minutes * 60))
                if not event.is_set():
                    try:
                        self.nudge_tracker.check(card_id, pending_since, _time.time(),
                                                 slack_sender=self.slack_sender)
                    except Exception:
                        pass
            threading.Thread(target=_nudge, daemon=True).start()
        event.wait(timeout=self.timeout_seconds)
        card = self.cards[card_id]
        verdict = card["verdict"] or "rejected"
        # Write the verdict back, so a card that timed out stops
        # being pending: without this, a timed-out card kept
        # verdict None forever and would never leave the list.
        card["verdict"] = verdict
        if verdict == "approved" and self.autonomy_tracker is not None:
            try:
                self.autonomy_tracker.record_approval(stake, _time.time())
            except Exception:
                pass
        if verdict == "rejected" and self.autonomy_tracker is not None:
            try:
                self.autonomy_tracker.record_rejection(stake)
            except Exception:
                pass
        return Decision(verdict)

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

    def approve(self, card_id: str, verdict: str) -> bool:
        if card_id not in self.cards:
            return False
        self.cards[card_id]["verdict"] = verdict
        self._events[card_id].set()
        return True


HUB_MAX_EVENTS = 500


# Inject the shared style into the full-page constants defined above.
CHAT_HTML = CHAT_HTML.replace("</style></head>", "</style><style>" + BASE_CSS + "</style></head>", 1)
DAG_HTML = DAG_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
REPLAY_HTML = REPLAY_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
VRM_HTML = VRM_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
FACTORY_HTML = FACTORY_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)


class WebUIServer:
    def __init__(self, home: Path, dispatcher: TriggerDispatcher | None = None,
                 auth_store=None, memory=None, whatsapp_reply=None,
                 slack_reply=None, whatsapp_verify_token: str = "",
                 slack_signing_secret: str = "", messaging_config=None,
                 whatsapp_sender=None, slack_sender=None, webhook_only: bool = False,
                 media_downloader=None, run_control=None, webui_token=None, transcribe_key=None,
                 tts_key=None, config=None):
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
        # Approval timeout comes from config when present, so
        # AIDON_APPROVAL_TIMEOUT_S actually changes the gate;
        # without a config the gate keeps its 180s default.
        timeout_seconds = getattr(config, "approval_timeout_seconds", 180)
        if timeout_seconds is None:
            timeout_seconds = 180
        self.webui_gate = WebUIGate(timeout_seconds=timeout_seconds)
        self.webhook_only = webhook_only
        self.media_downloader = media_downloader
        self.run_control = run_control
        self._web_runs: dict[str, dict] = {}
        self.webui_token = webui_token
        self.transcribe_key = transcribe_key
        self.tts_key = tts_key
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

    def start_goal(self, body: dict) -> dict:
        """Start the same dispatcher path used by messaging triggers."""
        from .run_control import new_run_id
        goal = str(body.get("goal", "")).strip()
        if not goal:
            return {"error": "goal is required"}
        project_id = str(body.get("project_id", "default") or "default")
        run_id = new_run_id()
        if self.run_control is not None:
            self.run_control.register(run_id, goal, project_id)
        record = {"run_id": run_id, "goal": goal, "project_id": project_id,
                  "status": "running", "events": [], "trace": []}
        self._web_runs[run_id] = record

        def reply(text):
            record["events"].append({"kind": "aidon-final", "text": str(text)})

        event = TriggerEvent(source="webui", kind="goal", goal=goal,
                             project_id=project_id,
                             payload={"run_id": run_id,
                                      "guidance": str(body.get("guidance", ""))},
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
        return {"run_id": run_id, "conversation_id": body.get("conversation_id", "")}

    def run_snapshot(self, run_id: str) -> dict | None:
        record = self._web_runs.get(run_id)
        control = self.run_control.get(run_id) if self.run_control is not None else None
        if record is None and control is None:
            return None
        out = dict(record or {})
        if control:
            out.update({"status": control.get("status", out.get("status", "running")),
                        "goal": control.get("goal", out.get("goal", ""))})
        out.setdefault("events", [])
        out.setdefault("trace", [])
        out["event_total"] = len(out["events"])
        out["trace_total"] = len(out["trace"])
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
            caption = text
            replies: list[str] = []
            # Image intake, same shape as parse_slack_event: image
            # files ride with the goal text as [image ...] markers.
            # Decision replies above already ran on the raw text, so
            # replacing the goal text here cannot change an approval
            # verdict. When the server's messaging config carries a
            # Slack bot token, the file bytes are downloaded here
            # (mirroring the WhatsApp path: library save + vision
            # message); without a token, markers + metadata only.
            payload: dict = {}
            try:
                from .slack_inbound import parse_slack_event as _parse_slack
                _parsed = _parse_slack(body)
                if _parsed is not None and _parsed.payload.get("images"):
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

    def projects_history(self) -> list[dict]:
        trace_dir = self.home / "trace"
        trace_count = len(list(trace_dir.glob("*.jsonl"))) \
            if trace_dir.exists() else 0
        return [{"project": project, "trace_files": trace_count}
                for project in self._projects()]

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
        "tts_voice_id": "AIDON_TTS_VOICE_ID",
        "backend": "AIDON_BACKEND",
        "api_endpoint": "AIDON_API_ENDPOINT",
        "embed_model": "AIDON_EMBED_MODEL",
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
        "ollama_endpoint": "AIDON_OLLAMA_ENDPOINT",
        "ollama_think": "AIDON_OLLAMA_THINK",
        "ollama_num_ctx": "AIDON_OLLAMA_NUM_CTX",
        "fast_num_ctx": "AIDON_FAST_NUM_CTX",
        "research_allowlist": "AIDON_RESEARCH_ALLOWLIST",
        "router_fast_phases": "AIDON_ROUTER_FAST_PHASES",
        "kernel": "AIDON_KERNEL",
    }
    _ENV_SECRET_MARKERS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "CLIENT")

    # Settings catalog: every live Config key, grouped, with type,
    # default, and secret flag. Dead old keys are deliberately
    # absent: no AIDON_META_*, no verifier model, no think phases,
    # no AIDON_DEPTH. Float-valued keys use type "string" (the
    # catalog has only string/int/bool/enum) and are validated
    # as numbers on write. Secrets are listed so the catalog
    # read can mask them and report is_set, never the value.
    SETTINGS_CATALOG = [
        {"group": "Model", "settings": [
            {"key": "AIDON_MODEL", "label": "Chat model", "type": "string", "default": "muse-spark", "desc": "Model id for the agent loop.", "secret": False},
            {"key": "AIDON_BACKEND", "label": "Model backend", "type": "enum", "default": "", "desc": "meta, ollama, http, or fake.", "secret": False, "options": ["meta", "ollama", "http", "fake"]},
            {"key": "AIDON_API_ENDPOINT", "label": "API endpoint", "type": "string", "default": "", "desc": "OpenAI-compatible endpoint, or Ollama when it points at localhost:11434.", "secret": False},
            {"key": "AIDON_API_KEY", "label": "API key", "type": "string", "default": "", "desc": "Key for the Meta Model API or other endpoint.", "secret": True},
            {"key": "AIDON_EMBED_MODEL", "label": "Embed model", "type": "string", "default": "google/embeddinggemma-2", "desc": "Embedding model for memory.", "secret": False},
            {"key": "AIDON_FAST_MODEL", "label": "Fast model", "type": "string", "default": "", "desc": "Small model for fast phases, else the main model.", "secret": False},
            {"key": "AIDON_FAST_NUM_CTX", "label": "Fast model context", "type": "int", "default": "0", "desc": "Context window for the fast model, 0 = backend default.", "secret": False},
            {"key": "AIDON_OLLAMA_ENDPOINT", "label": "Ollama endpoint", "type": "string", "default": "http://localhost:11434", "desc": "Local Ollama server.", "secret": False},
            {"key": "AIDON_OLLAMA_THINK", "label": "Ollama think", "type": "bool", "default": "0", "desc": "Enable Ollama chain-of-thought.", "secret": False},
            {"key": "AIDON_OLLAMA_NUM_CTX", "label": "Ollama context", "type": "int", "default": "0", "desc": "Context window for Ollama, 0 = backend default.", "secret": False},
            {"key": "AIDON_MODEL_TIMEOUT_S", "label": "Model timeout seconds", "type": "string", "default": "120", "desc": "Timeout for one model call, in seconds.", "secret": False},
            {"key": "AIDON_ROUTER_FAST_PHASES", "label": "Router fast phases", "type": "string", "default": "interpret,learn,respond", "desc": "Comma-separated phases routed to the fast model.", "secret": False},
            {"key": "AIDON_IMAGE_MODEL", "label": "Image model", "type": "string", "default": "muse-image-1.0", "desc": "Image generation model.", "secret": False},
            {"key": "AIDON_SAM_MODEL", "label": "SAM model", "type": "string", "default": "meta-sam", "desc": "Segmentation model for video editing.", "secret": False},
            {"key": "AIDON_MAX_TOKENS_PER_RUN", "label": "Max tokens per run", "type": "int", "default": "200000", "desc": "Hard token budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_STEPS", "label": "Max steps", "type": "int", "default": "50", "desc": "Hard step budget for one goal run.", "secret": False},
            {"key": "AIDON_MAX_WALLCLOCK_MIN", "label": "Max wallclock minutes", "type": "int", "default": "60", "desc": "Hard wallclock budget for one goal run, in minutes.", "secret": False},
            {"key": "AIDON_APPROVAL_TIMEOUT_S", "label": "Approval timeout seconds", "type": "int", "default": "180", "desc": "Seconds an approval card waits before failing closed.", "secret": False},
        ]},
        {"group": "Sandbox", "settings": [
            {"key": "AIDON_EXECUTION_BACKEND", "label": "Execution backend", "type": "enum", "default": "bubblewrap", "desc": "Sandbox backend for shell steps.", "secret": False, "options": ["bubblewrap", "docker", "subprocess"]},
            {"key": "AIDON_DOCKER_IMAGE", "label": "Docker image", "type": "string", "default": "python:3.12-slim", "desc": "Pinned image for the Docker backend.", "secret": False},
            {"key": "AIDON_DOCKER_NETWORK", "label": "Docker network", "type": "bool", "default": "0", "desc": "Allow network for the Docker backend.", "secret": False},
            {"key": "AIDON_MAX_SANDBOX_SECONDS", "label": "Sandbox timeout seconds", "type": "int", "default": "120", "desc": "Timeout for one sandbox command.", "secret": False},
            {"key": "AIDON_KERNEL", "label": "Kernel mode", "type": "bool", "default": "0", "desc": "Kernel mode: write/read/list/shell only, no calculate, no plugins.", "secret": False},
        ]},
        {"group": "Memory", "settings": [
            {"key": "AIDON_FORGET_HALF_LIFE_DAYS", "label": "Forget half-life days", "type": "string", "default": "21", "desc": "Forgetting-curve half-life, in days.", "secret": False},
            {"key": "AIDON_FORGET_GRACE_DAYS", "label": "Forget grace days", "type": "int", "default": "35", "desc": "Days before forgetting starts.", "secret": False},
            {"key": "AIDON_FORGET_CLEANUP_THRESHOLD", "label": "Forget cleanup threshold", "type": "string", "default": "0.02", "desc": "Recall weight below which a point is pruned.", "secret": False},
            {"key": "AIDON_INGEST_DIR", "label": "Ingest directory", "type": "string", "default": "", "desc": "Folder watched for ingest triggers.", "secret": False},
            {"key": "AIDON_QDRANT_URL", "label": "Qdrant URL", "type": "string", "default": "", "desc": "Qdrant server URL, empty = local mode.", "secret": False},
        ]},
        {"group": "Research", "settings": [
            {"key": "AIDON_SEARCH_BACKEND", "label": "Search backend", "type": "enum", "default": "tavily", "desc": "Web search backend.", "secret": False, "options": ["tavily", "searxng"]},
            {"key": "AIDON_SEARXNG_URL", "label": "SearXNG URL", "type": "string", "default": "http://localhost:8888", "desc": "SearXNG server for the searxng backend.", "secret": False},
            {"key": "AIDON_TAVILY_API_KEY", "label": "Tavily API key", "type": "string", "default": "", "desc": "Key for Tavily search.", "secret": True},
            {"key": "AIDON_RESEARCH_MAX_ROUNDS", "label": "Research max rounds", "type": "int", "default": "10", "desc": "Max research rounds per run.", "secret": False},
            {"key": "AIDON_MAX_STUCK_ITERATIONS", "label": "Max stuck iterations", "type": "int", "default": "8", "desc": "Iterations without progress before the run stops.", "secret": False},
            {"key": "AIDON_PARALLEL_TOOLS", "label": "Parallel tools", "type": "int", "default": "4", "desc": "Parallel research/tool workers.", "secret": False},
            {"key": "AIDON_RESEARCH_ALLOWLIST", "label": "Research allowlist", "type": "string", "default": "", "desc": "Comma-separated domains research may use, empty = unrestricted.", "secret": False},
            {"key": "AIDON_YOUTUBE_API_KEY", "label": "YouTube API key", "type": "string", "default": "", "desc": "Key for YouTube search.", "secret": True},
        ]},
        {"group": "Messaging", "settings": [
            {"key": "AIDON_WHATSAPP_TOKEN", "label": "WhatsApp token", "type": "string", "default": "", "desc": "WhatsApp Cloud API token.", "secret": True},
            {"key": "AIDON_WHATSAPP_PHONE_ID", "label": "WhatsApp phone id", "type": "string", "default": "", "desc": "WhatsApp Cloud phone number id.", "secret": False},
            {"key": "AIDON_WHATSAPP_VERIFY_TOKEN", "label": "WhatsApp verify token", "type": "string", "default": "", "desc": "Token WhatsApp uses to verify the webhook.", "secret": True},
            {"key": "AIDON_WHATSAPP_APP_SECRET", "label": "WhatsApp app secret", "type": "string", "default": "", "desc": "App secret for WhatsApp signature checks.", "secret": True},
            {"key": "AIDON_WHATSAPP_OWNER", "label": "WhatsApp owner", "type": "string", "default": "", "desc": "Owner phone number for the WhatsApp owner gate.", "secret": False},
            {"key": "AIDON_SLACK_BOT_TOKEN", "label": "Slack bot token", "type": "string", "default": "", "desc": "Slack bot token for inbound/outbound Slack.", "secret": True},
            {"key": "AIDON_SLACK_SIGNING_SECRET", "label": "Slack signing secret", "type": "string", "default": "", "desc": "Slack signing secret for webhook verification.", "secret": True},
            {"key": "AIDON_THREADS_ACCOUNT_ID", "label": "Threads account id", "type": "string", "default": "", "desc": "Threads account id for publishing.", "secret": False},
            {"key": "AIDON_THREADS_ACCESS_TOKEN", "label": "Threads access token", "type": "string", "default": "", "desc": "Threads access token for publishing.", "secret": True},
            {"key": "AIDON_IG_USER_ID", "label": "Instagram user id", "type": "string", "default": "", "desc": "Instagram user id for publishing.", "secret": False},
            {"key": "AIDON_IG_ACCESS_TOKEN", "label": "Instagram access token", "type": "string", "default": "", "desc": "Instagram access token for publishing.", "secret": True},
            {"key": "AIDON_GITHUB_TOKEN", "label": "GitHub token", "type": "string", "default": "", "desc": "GitHub token for repository tools.", "secret": True},
            {"key": "AIDON_OWNER_EMAIL", "label": "Owner email", "type": "string", "default": "", "desc": "Where drafts and reports to the owner are addressed.", "secret": False},
            {"key": "AIDON_DAILY_REPORT_TIME", "label": "Daily report time", "type": "string", "default": "", "desc": "Time of day for the daily report.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_MIN", "label": "Approval nudge minutes", "type": "int", "default": "15", "desc": "Minutes before an approval card is nudged.", "secret": False},
            {"key": "AIDON_APPROVAL_NUDGE_CHANNEL", "label": "Approval nudge channel", "type": "string", "default": "", "desc": "Channel approval nudges go to.", "secret": False},
            {"key": "AIDON_CORRECTED_SUBJECT", "label": "Corrected subject", "type": "bool", "default": "1", "desc": "Mark corrected emails with a Corrected subject.", "secret": False},
            {"key": "AIDON_AUTONOMY_ENABLED", "label": "Autonomy enabled", "type": "bool", "default": "1", "desc": "Trust-based autonomy for repeated skills.", "secret": False},
            {"key": "AIDON_AUTONOMY_THRESHOLD", "label": "Autonomy threshold", "type": "int", "default": "5", "desc": "Successes before a skill graduates a trust tier.", "secret": False},
            {"key": "AIDON_AUTONOMY_DECAY_DAYS", "label": "Autonomy decay days", "type": "string", "default": "14", "desc": "Days after which trust decays.", "secret": False},
        ]},
        {"group": "WebUI", "settings": [
            {"key": "AIDON_WEBUI_TOKEN", "label": "WebUI token", "type": "string", "default": "", "desc": "Bearer token for the WebUI API.", "secret": True},
            {"key": "AIDON_TIMEZONE", "label": "Timezone", "type": "string", "default": "UTC", "desc": "Timezone for schedules and reports.", "secret": False},
            {"key": "AIDON_TTS_VOICE_ID", "label": "TTS voice id", "type": "string", "default": "", "desc": "Voice id for text-to-speech.", "secret": False},
            {"key": "AIDON_FISH_API_KEY", "label": "Fish API key", "type": "string", "default": "", "desc": "Key for Fish TTS.", "secret": True},
            {"key": "AIDON_TRANSCRIBE_API_KEY", "label": "Transcribe API key", "type": "string", "default": "", "desc": "Key for voice transcription.", "secret": True},
        ]},
    ]
    _CATALOG_FLOAT_KEYS = frozenset({
        "AIDON_MODEL_TIMEOUT_S", "AIDON_FORGET_HALF_LIFE_DAYS",
        "AIDON_FORGET_CLEANUP_THRESHOLD", "AIDON_AUTONOMY_DECAY_DAYS",
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

        A secret entry returns value "" and is_set, never the
        value itself. A non-secret returns its .env value, else
        its process-environment value, else its default, with
        from_file saying whether .env set it.
        """
        import os
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
                raw = file_vals.get(entry["key"])
                if raw is None:
                    raw = os.environ.get(entry["key"])
                item["from_file"] = entry["key"] in file_vals
                if entry.get("secret"):
                    item["value"] = ""
                    item["is_set"] = bool(raw)
                else:
                    item["value"] = raw if raw is not None else entry.get("default", "")
                items.append(item)
            groups.append({"group": group["group"], "settings": items})
        return {"env_file": str(self.home / ".env"), "groups": groups}

    def settings_set_env(self, key: str, value) -> dict:
        """Write one setting to <home>/.env, secrets included.

        Old-Aidon behaviour: the settings page can enter
        everything, keys included. Accepts the friendly settings
        name (e.g. "timezone") or the env var name itself (e.g.
        "AIDON_TIMEZONE"), for every catalog key plus the original
        19 friendly names. Type validation: int must parse as int,
        bool must be one of 0/1/true/false/yes/no (stored as 1/0),
        enum must be in options, float-valued keys must parse as a
        number. A secret key with an empty value is a no-op
        ("don't change") and must never clear the key; a secret
        with a value is written, write-only — the catalog never
        returns it back (value "" plus is_set). Unknown keys and
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

    def handle_project_delete(self, project_id: str) -> dict:
        """Delete a project: memory points, conversations, brief.

        Returns ok True only when the project existed somewhere
        (memory projects, the SQLite conversation store, legacy
        JSON conversations, or a saved brief). An unknown project
        returns ok False. With no memory attached, memory deletion
        is skipped but conversations/briefs still delete, and ok
        reflects whether anything was found.
        """
        project_id = str(project_id or "").strip()
        if not project_id:
            return {"ok": False, "error": "project_id is required"}
        found = False
        removed_memory = 0
        if self.memory is not None:
            try:
                projects = self.memory.list_projects()
            except Exception:
                projects = []
            if project_id in projects:
                found = True
            # Memory's project-deletion API is forget_project;
            # store-level objects expose delete_by_project per
            # collection instead. Use whichever is present.
            try:
                if hasattr(self.memory, "forget_project"):
                    deleted = self.memory.forget_project(project_id) or {}
                    removed_memory += int(sum(int(v) for v in deleted.values()))
                elif hasattr(self.memory, "delete_by_project"):
                    for collection in ("working", "episodic", "semantic", "experience", "knowledge"):
                        removed_memory += int(self.memory.delete_by_project(collection, project_id) or 0)
                elif hasattr(getattr(self.memory, "store", None), "delete_by_project"):
                    for collection in ("working", "episodic", "semantic", "experience", "knowledge"):
                        removed_memory += int(self.memory.store.delete_by_project(collection, project_id) or 0)
            except Exception:
                pass
            if removed_memory:
                found = True
        removed_conversations = 0
        db_path = self.home / "conversations.db"
        if db_path.is_file():
            try:
                import sqlite3
                conn = sqlite3.connect(str(db_path), timeout=10.0)
                try:
                    rows = conn.execute(
                        "SELECT id FROM conversations WHERE project_id = ?",
                        (project_id,)).fetchall()
                    ids = [row[0] for row in rows]
                    if ids:
                        found = True
                        removed_conversations = len(ids)
                        placeholders = ",".join("?" for _ in ids)
                        conn.execute(
                            f"DELETE FROM exchanges WHERE conversation_id IN ({placeholders})", ids)
                        conn.execute(
                            f"DELETE FROM attachments WHERE conversation_id IN ({placeholders})", ids)
                        try:
                            conn.execute(
                                f"DELETE FROM summaries WHERE conversation_id IN ({placeholders})", ids)
                        except Exception:
                            pass
                        conn.execute(
                            "DELETE FROM conversations WHERE project_id = ?", (project_id,))
                        conn.commit()
                finally:
                    conn.close()
            except Exception:
                pass
        for path in sorted((self.home / "conversations").glob("*.json")) if (self.home / "conversations").is_dir() else []:
            try:
                data = self._read_json_file(path)
            except Exception:
                data = None
            if data is not None and str(data.get("project_id", "")) == project_id:
                found = True
                try:
                    path.unlink()
                except OSError:
                    pass
        brief_path = self.home / "project_briefs" / f"{self._safe_name(project_id)}.json"
        if brief_path.is_file():
            found = True
            try:
                brief_path.unlink()
            except OSError:
                pass
        if not found:
            return {"ok": False, "error": f"unknown project: {project_id}"}
        return {"ok": True, "project_id": project_id,
                "removed_memory_points": removed_memory,
                "removed_conversations": removed_conversations}

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

    # -- Group D additions: tools, conversations, briefs, ideas, hub runs,
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
        registry = PluginRegistry(self.home)
        registry.load_all()
        plugin_tools = registry.tool_names()
        return {"sandbox_tools": ["write", "read", "list", "shell", "calculate"],
                "goal_tools": all_tool_names(plugin_tools),
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
        return {"conversations": records}

    def handle_conversation_create(self, body: dict) -> dict:
        try:
            db = self._conversations_db()
            record = db.create_conversation(str(body.get("project_id", "default")))
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
                      "project_id": str(body.get("project_id", "default")),
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
                return {"ok": True}
        except Exception:
            pass
        path = self.home / "conversations" / f"{self._safe_name(conversation_id)}.json"
        if not path.is_file():
            return {"ok": False}
        path.unlink()
        return {"ok": True}

    def handle_brief_get(self, project_id: str):
        return self._read_json_file(
            self.home / "project_briefs" / f"{self._safe_name(project_id)}.json")

    def handle_brief_save(self, project_id: str, body: dict) -> dict:
        record = {"project_id": project_id, "brief": body.get("brief", body)}
        self._write_json_file(
            self._json_dir("project_briefs") / f"{self._safe_name(project_id)}.json",
            record)
        return record

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
        result = _tts(text, tts_key,
                      str(body.get("voice_id", "")))
        if not result.get("ok"):
            return 500, {"error": result.get("error", "TTS failed")}
        return 200, {"ok": True,
                     "audio_bytes": len(result.get("audio", b"")),
                     "_audio": result.get("audio", b"")}

    def handle_goal_with_images(self, body: dict) -> dict:
        import base64 as _b64
        goal = str(body.get("goal", ""))
        library_dir = self._json_dir("library")
        saved = []
        for image in body.get("images", []) or []:
            name = self._safe_name(image.get("name", "image.png"))
            try:
                data = _b64.b64decode(str(image.get("data_b64", "")))
            except Exception:
                continue
            (library_dir / name).write_bytes(data)
            saved.append(name)
        return {"goal": goal, "images": saved,
                "note": "JSON variant of the multipart goals-with-images endpoint; "
                        "no run is started by this route."}

    # -- routes added for old-Aidon WebUI parity -------------------------
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

    def handle_voice(self) -> dict:
        """Voice capability info: whether spoken replies are
        announced, and whether a TTS key is configured. No secret
        is ever returned, only booleans.
        """
        return {"announce": bool(getattr(self, "voice_announce", False)),
                "tts_available": bool(getattr(self, "tts_key", None))}

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
        """Mission records stored under <home>/missions/<project>/
        mission.json. No missions is an empty list, never an error;
        a directory without a mission.json is skipped.
        """
        records = []
        missions_dir = self.home / "missions"
        if missions_dir.is_dir():
            for project_dir in sorted(missions_dir.iterdir()):
                if not project_dir.is_dir():
                    continue
                data = self._read_json_file(project_dir / "mission.json")
                if data is not None:
                    records.append(data)
        return {"missions": records}

    def handle_capabilities(self) -> dict:
        """One view over what Aidon can do: sandbox + plugin tool
        names, skill library names, and MCP server names. A source
        that is missing or unreadable contributes an empty list,
        never an error, so the route works on a fresh home.
        """
        try:
            tools = self.handle_tools()
            tool_names = list(tools.get("goal_tools", []))
        except Exception:
            tool_names = []
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
        return {"tools": tool_names, "skills": skill_names,
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
            if wanted in all_tool_names():
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
        except Exception:
            return {"points": []}
        out = []
        for point in points:
            out.append({"id": getattr(point, "id", ""),
                        "text": getattr(point, "text", ""),
                        "payload": dict(getattr(point, "payload", {}) or {})})
        return {"points": out}

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
                """Serve only files from the bundled old-Aidon WebUI assets."""
                candidate = (STATIC_DIR / relative).resolve()
                root = STATIC_DIR.resolve()
                if root not in candidate.parents or not candidate.is_file():
                    self._send_json(404, {"error": "not found"})
                    return
                data = candidate.read_bytes()
                if candidate.name in {"index.html", "dag.html", "capabilities.html",
                                      "memory.html", "knowledge.html", "reports.html",
                                      "settings.html"}:
                    version = str(int(candidate.stat().st_mtime)).encode()
                    data = data.replace(b"__ASSET_V__", version)
                content_type = mimetypes.guess_type(str(candidate))[0] or \
                    "application/octet-stream"
                self._send_bytes(200, content_type, data)

            def _read_body(self) -> bytes:
                length = int(self.headers.get("Content-Length", 0) or 0)
                return self.rfile.read(length) if length else b""

            def do_GET(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith("/webhook/"):
                    from .webui_auth import is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                if server.webhook_only and parsed.path != "/webhook/whatsapp":
                    self._send_json(404, {"error": "webhook only"})
                elif parsed.path == "/static" or parsed.path.startswith("/static/"):
                    relative = parsed.path[len("/static/"):] if parsed.path.startswith("/static/") else ""
                    self._send_static(relative)
                elif parsed.path == "/":
                    self._send_static("index.html")
                elif parsed.path in ("/dag", "/capabilities", "/memory", "/knowledge",
                                     "/reports", "/settings"):
                    self._send_static(parsed.path.lstrip("/") + ".html")
                elif parsed.path == "/settings":
                    keys = server.auth_store.list_keys() if server.auth_store else []
                    html = """<h1>Settings</h1>
<p>Every knob, one page. Changes are written to <span id="env-path"></span>.
Secret keys (API keys) are shown only as set or unset, never shown back —
type a new value to set one, leave it empty to keep the current one.
Restart the server for changes to take effect.</p>
<div id="settings-groups"></div>
<p><button id="settings-save">Save settings</button> <span id="settings-status"></span></p>
<h2>Keys, hash prefixes only, never real keys</h2>"""
                    for key in keys:
                        html += f"<p>{key['name']} {key['scope']} {key['key_hash_prefix']}</p>"
                    html += """<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
async function loadSettings(){
  const r = await fetch('/api/settings/catalog'); const data = await r.json();
  document.getElementById('env-path').textContent = data.env_file || '';
  let out = '';
  (data.groups || []).forEach(function(g){
    out += '<h2>' + esc(g.group) + '</h2>';
    g.settings.forEach(function(s){
      out += '<p><label>' + esc(s.label) + ' (' + esc(s.key) + ')</label><br>';
      if (s.type === 'bool') {
        out += '<input type="checkbox" data-key="' + esc(s.key) + '"' + ((s.value === '1' || s.value === 'true') ? ' checked' : '') + '>';
      } else if (s.type === 'enum') {
        out += '<select data-key="' + esc(s.key) + '">' + (s.options || []).map(function(o){ return '<option' + (o === s.value ? ' selected' : '') + '>' + esc(o) + '</option>'; }).join('') + '</select>';
      } else if (s.secret) {
        out += '<input type="password" data-key="' + esc(s.key) + '" placeholder="' + (s.is_set ? 'set - leave empty to keep, type to replace' : 'not set - type a value to set') + '">';
      } else {
        out += '<input type="text" data-key="' + esc(s.key) + '" value="' + esc(s.value) + '">';
      }
      if (s.desc) { out += '<br><small>' + esc(s.desc) + '</small>'; }
      out += '</p>';
    });
  });
  document.getElementById('settings-groups').innerHTML = out;
}
async function saveSettings(){
  const status = document.getElementById('settings-status'); status.textContent = 'Saving...';
  const fields = document.querySelectorAll('[data-key]'); let failed = 0;
  for (const f of fields) {
    let v = (f.type === 'checkbox') ? (f.checked ? '1' : '0') : f.value;
    if (f.type === 'password' && !v) { continue; }
    const r = await fetch('/api/settings/env', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: f.getAttribute('data-key'), value: v})});
    const d = await r.json();
    if (!d.ok) { failed++; status.textContent = 'Failed: ' + (d.error || 'unknown'); }
  }
  if (!failed) { status.textContent = 'Saved. Restart the server for changes to take effect.'; }
}
document.getElementById('settings-save').addEventListener('click', saveSettings);
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
                    project_id = query.get("project_id", [""])[0]
                    items = Library(server.home).list_items(project_id)
                    # Keep the legacy file listing visible for homes created
                    # before the library manifest was introduced.
                    if not items:
                        items = server._library_files()
                    self._send_json(200, {"files": items})
                elif parsed.path == "/api/projects":
                    self._send_json(200, {"projects": server._projects()})
                elif parsed.path == "/api/approvals":
                    # Auth already checked at the top of do_GET,
                    # the same is_authorized check every /api/
                    # route gets. Only still-pending cards: a
                    # decided or timed-out card is gone here.
                    self._send_json(200, {"cards": server.webui_gate.pending_cards()})
                elif parsed.path == "/factory":
                    self._send_html(200, FACTORY_HTML)
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
                elif parsed.path == "/capabilities":
                    from ..capabilities.plugins import PluginRegistry as _PR
                    _reg = _PR(server.home); _reg.load_all()
                    html = "<h1>Capabilities</h1><p>What Aidon can do, what it has learned, and the evidence.</p>"
                    html += f"<p>Sandbox tools: write, read, list, shell, calculate</p><p>Plugin tools: {', '.join(_reg.tool_names())}</p>"
                    html += "<p>Computer use: screenshot and click, type, scroll, key, approval-gated, no driver configured by default, no GUI action without a driver.</p>"
                    html += "<h2>Capabilities studio</h2><p>Record a skill success or failure, or probe a plugin dir, or edit a skill's description, via POST /api/studio/capabilities with JSON {action: success|failure|probe|edit, name, path, description}.</p>"
                    html += "<form onsubmit=\"event.preventDefault(); fetch('/api/studio/capabilities',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:document.getElementById('cap_action').value,name:document.getElementById('cap_name').value,path:document.getElementById('cap_path').value,description:document.getElementById('cap_desc').value})}).then(r=>r.json()).then(d=>document.getElementById('cap_result').textContent=JSON.stringify(d));\"><select id=\"cap_action\"><option>success</option><option>failure</option><option>probe</option></select> <input id=\"cap_name\" placeholder=\"skill name\"> <input id=\"cap_path\" placeholder=\"plugin dir for probe\"> <button>Run</button></form><pre id=\"cap_result\"></pre>"
                    self._send_html(200, _page_html("Capabilities", html))
                elif parsed.path == "/memory":
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
                elif parsed.path == "/knowledge":
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
                elif parsed.path == "/reports":
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
                elif parsed.path == "/api/projects/history":
                    self._send_json(200, {"projects": server.projects_history()})
                elif parsed.path == "/api/settings/catalog":
                    self._send_json(200, server.settings_catalog())
                elif parsed.path == "/api/settings":
                    self._send_json(200, server.settings_get())
                elif parsed.path == "/api/avatar.vrm":
                    vrm_path = server.home / "avatar.vrm"
                    if not vrm_path.exists():
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
                elif parsed.path == "/api/tools":
                    self._send_json(200, server.handle_tools())
                elif parsed.path.startswith("/api/runs/") and "/" not in parsed.path[len("/api/runs/"):]:
                    run_id = parsed.path[len("/api/runs/"):]
                    data = server.run_snapshot(run_id)
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "run not found"})
                elif parsed.path == "/api/conversations":
                    self._send_json(200, server.handle_conversations_list())
                elif parsed.path.startswith("/api/conversations/"):
                    conv_id = parsed.path[len("/api/conversations/"):]
                    data = server.handle_conversation_get(conv_id)
                    self._send_json(200 if data is not None else 404, data or {"error": "not found"})
                elif parsed.path.startswith("/api/projects/") and parsed.path.endswith("/brief"):
                    project_id = parsed.path[len("/api/projects/"):-len("/brief")].strip("/")
                    data = server.handle_brief_get(project_id)
                    self._send_json(200 if data is not None else 404, data or {"error": "not found"})
                elif parsed.path == "/api/ideas":
                    self._send_json(200, server.handle_ideas_list())
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
                elif parsed.path == "/api/reports":
                    self._send_json(200, server.handle_reports_list())
                elif parsed.path.startswith("/api/reports/"):
                    day = parsed.path[len("/api/reports/"):].strip("/")
                    data = server.handle_report_get(day)
                    self._send_json(200 if data is not None else 404,
                                    data or {"error": "not found"})
                elif parsed.path == "/api/missions":
                    self._send_json(200, server.handle_missions_list())
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
                    self._send_json(200, {"items": page, "total": len(raw_points),
                                          "matched": matched, "mode": "Aidon memory"})
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
                if not parsed.path.startswith("/webhook/"):
                    from .webui_auth import is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                if server.webhook_only and parsed.path not in ("/webhook/whatsapp", "/webhook/slack"):
                    self._send_json(404, {"error": "webhook only"})
                    return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
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
                elif parsed.path == "/api/transcribe":
                    if not server.transcribe_key:
                        self._send_json(501, {"error": "transcribe key not configured"})
                        return
                    import base64 as _b64t
                    try:
                        audio = _b64t.b64decode(str(body.get("audio_b64", "")))
                        from ..capabilities.voice import transcribe as _transcribe
                        result = _transcribe(audio, server.transcribe_key)
                        self._send_json(200, result)
                    except Exception as exc:
                        self._send_json(500, {"error": str(exc)})
                elif parsed.path == "/api/goals":
                    result = server.start_goal(body)
                    self._send_json(200 if "run_id" in result else 400, result)
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/approval"):
                    run_id = parsed.path[len("/api/runs/"):-len("/approval")].strip("/")
                    pending = server.webui_gate.pending_cards()
                    if not pending:
                        self._send_json(409, {"error": "no pending approval"})
                    else:
                        decision = str(body.get("decision", body.get("verdict", "rejected")))
                        if decision == "edited":
                            decision = "approved"
                        ok = server.webui_gate.approve(pending[0]["id"], decision)
                        self._send_json(200 if ok else 409, {"ok": ok, "run_id": run_id})
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/steer"):
                    run_id = parsed.path[len("/api/runs/"):-len("/steer")].strip("/")
                    ok = server.run_control.steer(run_id, str(body.get("note", ""))) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
                elif parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                    run_id = parsed.path[len("/api/runs/"):-len("/cancel")].strip("/")
                    ok = server.run_control.cancel(run_id, gate=server.webui_gate) if server.run_control else False
                    self._send_json(200 if ok else 404, {"ok": ok})
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
                    ok = server.webui_gate.approve(card_id, verdict)
                    self._send_json(200 if ok else 404, {"ok": ok})
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
                elif parsed.path == "/api/goals/with-images":
                    self._send_json(200, server.handle_goal_with_images(body))
                elif parsed.path == "/api/plugins/adopt":
                    result = server.handle_plugin_adopt(str(body.get("path", "")))
                    self._send_json(200 if result.get("ok") else 400, result)
                elif parsed.path == "/webhook/whatsapp":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_whatsapp_post(body, raw, headers)
                    self._send_json(status, payload)
                elif parsed.path == "/webhook/slack":
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    status, payload = server.handle_slack_post(raw, headers, body)
                    # Slack Events API URL verification expects the
                    # challenge in a JSON response body.
                    self._send_json(status, payload)
                else:
                    self._send_json(404, {"error": "not found"})

            def do_DELETE(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith("/webhook/"):
                    from .webui_auth import is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                if parsed.path.startswith("/api/memory/episodes/") or \
                        parsed.path.startswith("/api/memory/knowledge/"):
                    parts = parsed.path.split("/")
                    collection = parts[3]
                    point_id = "/".join(parts[4:]).strip("/")
                    result = server.handle_memory_point_delete(collection, point_id)
                    self._send_json(200 if result["ok"] else 404, result)
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
                elif parsed.path.startswith("/api/projects/"):
                    project_id = parsed.path[len("/api/projects/"):].strip("/")
                    if project_id and "/" not in project_id:
                        result = server.handle_project_delete(project_id)
                        self._send_json(200 if result["ok"] else 404, result)
                    else:
                        self._send_json(404, {"error": "not found"})
                else:
                    self._send_json(404, {"error": "not found"})

            def do_PUT(self):
                parsed = urlparse(self.path)
                if not parsed.path.startswith("/webhook/"):
                    from .webui_auth import is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
                raw = self._read_body()
                try:
                    body = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    body = {}
                if parsed.path.startswith("/api/projects/") and parsed.path.endswith("/brief"):
                    project_id = parsed.path[len("/api/projects/"):-len("/brief")].strip("/")
                    self._send_json(200, server.handle_brief_save(project_id, body))
                else:
                    self._send_json(404, {"error": "not found"})

            def log_message(self, *args):
                pass

        return Handler

    def serve(self, port: int = 8002) -> None:
        # Threading, so a goal run waiting on an approval card
        # does not block the /api/approve request that resolves it.
        ThreadingHTTPServer(("127.0.0.1", port), self.make_handler()).serve_forever()
