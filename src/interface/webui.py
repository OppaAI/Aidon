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
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>" + title +
            "</title><link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head><body><script>try{if(localStorage.getItem('aidon-theme')==='dark'){document.body.classList.add('dark')}}catch(e){}</script>" + body + "</body></html>")


CHAT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon</title>
<style>
#avatar { width: 220px; }
#robot { transform-origin: 100px 190px; }
#robot-head { transform-origin: 100px 54px; }
#robot-arm-left { transform-origin: 52px 111px; }
#robot-arm-right { transform-origin: 148px 111px; }
#robot { animation: robot-bob 5s ease-in-out infinite; }
#robot-head { animation: robot-head-sway 7s ease-in-out infinite; }
#robot-arm-left, #robot-arm-right { animation: robot-arm-sway 4.8s ease-in-out infinite; }
@keyframes robot-bob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-2px); } }
@keyframes robot-head-sway { 0%,100% { transform: rotate(0); } 35% { transform: rotate(-2deg); } 70% { transform: rotate(1.5deg); } }
@keyframes robot-arm-sway { 0%,100% { transform: rotate(0); } 50% { transform: rotate(2.5deg); } }
.robot-eye { animation: robot-blink 4.2s infinite; transform-origin: center; transform-box: fill-box; }
@keyframes robot-blink { 0%,92%,98%,100% { transform: scaleY(1); } 94%,96% { transform: scaleY(0.1); } }
@media (prefers-reduced-motion: reduce) { #robot, #robot-head, #robot-arm-left, #robot-arm-right, .robot-eye { animation: none; } }
#mouth-smile { opacity: 1; }
#mouth-open { opacity: 0; }
#wave-bars span { display: inline-block; width: 6px; background: var(--accent); margin-right: 2px; vertical-align: bottom; height: 4px; }
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
<defs>
<radialGradient id="robot-shell" cx="35%" cy="18%" r="85%"><stop offset="0" stop-color="#fff"/><stop offset=".46" stop-color="#f3f5f4"/><stop offset=".82" stop-color="#d7deeb"/><stop offset="1" stop-color="#aebbd4"/></radialGradient>
<linearGradient id="robot-arm-shell" x1="0" y1="0" x2="1" y2=".25"><stop offset="0" stop-color="#c4d5e8"/><stop offset=".32" stop-color="#fff"/><stop offset=".72" stop-color="#eef1f4"/><stop offset="1" stop-color="#bbc9dd"/></linearGradient>
<radialGradient id="robot-head-shell" cx="34%" cy="14%" r="88%"><stop offset="0" stop-color="#fff"/><stop offset=".52" stop-color="#f5f5e8"/><stop offset=".84" stop-color="#dce1e6"/><stop offset="1" stop-color="#b9c4d4"/></radialGradient>
<linearGradient id="robot-face" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#263a60"/><stop offset=".55" stop-color="#1a2a4a"/><stop offset="1" stop-color="#101b32"/></linearGradient>
<linearGradient id="robot-ear" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#7899bc"/><stop offset=".38" stop-color="#a9c8df"/><stop offset=".68" stop-color="#7095bc"/><stop offset="1" stop-color="#486b96"/></linearGradient>
</defs>
<g id="robot-arm-left">
<path d="M 68 103 Q 53 103 47 117 Q 41 134 35 150 Q 33 159 37 163 Q 40 167 45 165 Q 50 162 53 156 Q 64 140 72 124 Z" fill="url(#robot-arm-shell)" stroke="#c6d0df" stroke-width="1.5"/>
<path d="M 54 117 Q 49 135 41 153 Q 39 158 42 159 Q 46 157 49 150 L 64 127 Z" fill="#fff" opacity=".62"/>
<path d="M39 162 L39 164 M44 163 L44 165 M49 161 L50 163" fill="none" stroke="#1a2a4a" stroke-width="2.2" stroke-linecap="round"/>
</g>
<g id="robot-arm-right">
<path d="M 132 103 Q 147 103 153 117 Q 159 134 165 150 Q 167 159 163 163 Q 160 167 155 165 Q 150 162 147 156 Q 136 140 128 124 Z" fill="url(#robot-arm-shell)" stroke="#c6d0df" stroke-width="1.5"/>
<path d="M 146 117 Q 151 135 159 153 Q 161 158 158 159 Q 154 157 151 150 L 136 127 Z" fill="#fff" opacity=".62"/>
<path d="M161 162 L161 164 M156 163 L156 165 M151 161 L150 163" fill="none" stroke="#1a2a4a" stroke-width="2.2" stroke-linecap="round"/>
</g>
<g id="robot-head">
<g transform="translate(6 0) scale(.94 1)">
<rect x="36" y="40" width="12" height="32" rx="6" fill="url(#robot-ear)" stroke="#7894b5" stroke-width="1"/>
<rect x="152" y="40" width="12" height="32" rx="6" fill="url(#robot-ear)" stroke="#7894b5" stroke-width="1"/>
<path d="M38 48 Q39 43 42 44 M154 48 Q155 43 158 44" fill="none" stroke="#e5f4ff" stroke-width="1.5" stroke-linecap="round" opacity=".58"/>
<rect x="48" y="18" width="104" height="72" rx="21" fill="url(#robot-head-shell)" stroke="#c4cedb" stroke-width="1.5"/>
<path d="M100 18 L100 9" fill="none" stroke="#1a2a4a" stroke-width="2" stroke-linecap="round"/>
<circle cx="100" cy="6.5" r="3.2" fill="#7de9ff" stroke="#1a2a4a" stroke-width="1.2"/>
<path d="M55 39 Q58 29 71 28 H129 Q142 29 145 40" fill="none" stroke="#fff" stroke-width="3" stroke-linecap="round" opacity=".68"/>
<rect x="59" y="28" width="82" height="52" rx="14" fill="url(#robot-face)" stroke="#15223b" stroke-width="1"/>
<path d="M68 33 Q74 30 84 30 H128 Q135 31 137 36" fill="none" stroke="#6680a5" stroke-width="1.5" stroke-linecap="round" opacity=".4"/>
<path d="M70 38 Q76 31 88 31 H121 Q130 31 134 36 Q114 33 96 42 Q82 49 71 47 Z" fill="#d9f2ff" opacity=".2"/>
<path d="M71 39 Q78 33 89 33 H112 Q95 36 82 43 Q75 46 71 45 Z" fill="#fff" opacity=".16"/>
<circle class="robot-eye" id="eye-left" cx="85" cy="50" r="7.5" fill="none" stroke="#7de9ff" stroke-width="3"/>
<circle class="robot-eye" id="eye-right" cx="115" cy="50" r="7.5" fill="none" stroke="#7de9ff" stroke-width="3"/>
<path id="mouth-smile" d="M 90 67 Q 100 73 110 67" fill="none" stroke="#7de9ff" stroke-width="3" stroke-linecap="round"/>
<path id="mouth-open" d="M94 64 Q100 62 106 64 Q109 68 106 73 Q100 77 94 73 Q91 68 94 64Z" fill="#7de9ff" opacity="0"/>
</g>
</g>
<rect x="89" y="88" width="22" height="14" rx="6" fill="#1a2a4a"/>
<g id="robot-body">
<ellipse cx="69" cy="108" rx="6" ry="7" fill="#1a2a4a"/>
<ellipse cx="131" cy="108" rx="6" ry="7" fill="#1a2a4a"/>
<path d="M74 104 C82 101 118 101 126 104 C140 119 139 153 127 173 C119 185 108 190 100 190 C92 190 81 185 73 173 C61 153 60 119 74 104 Z" fill="url(#robot-shell)" stroke="#c6d0df" stroke-width="1.5"/>
<ellipse cx="81" cy="119" rx="13" ry="6" fill="#fff" opacity=".72"/>
<path d="M71 151 C82 164 118 164 129 151 Q124 173 109 181 Q100 186 91 181 Q76 173 71 151 Z" fill="#aebbd4" opacity=".24"/>
<path d="M81 177 Q100 188 119 177 Q114 187 100 188 Q86 187 81 177Z" fill="#1a2a4a" opacity=".92"/>
</g>
</g>
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
const mouthShapes = {
  closed: 'M90 67 Q100 73 110 67',
  ah: 'M94 64 Q100 62 106 64 Q109 68 106 73 Q100 77 94 73 Q91 68 94 64Z',
  ee: 'M91 66 Q100 64 109 66 Q108 70 100 72 Q92 70 91 66Z',
  oh: 'M96 63 Q100 61 104 63 Q108 67 105 73 Q100 77 95 73 Q92 67 96 63Z',
  oo: 'M97 64 Q100 62 103 64 Q106 67 104 72 Q100 75 96 72 Q94 67 97 64Z',
  fv: 'M92 67 Q100 66 108 67 L106 70 Q100 71 94 70Z',
  teeth: 'M93 65 Q100 63 107 65 L106 70 Q100 72 94 70Z',
};
function mouthForChar(ch) {
  if (/[mbp]/i.test(ch)) return 'closed';
  if (/[fv]/i.test(ch)) return 'fv';
  if (/[aáàâäãå]/i.test(ch)) return 'ah';
  if (/[eéèêëiyíìîï]/i.test(ch)) return 'ee';
  if (/[oóòôöõ]/i.test(ch)) return 'oh';
  if (/[uúùûüw]/i.test(ch)) return 'oo';
  return /[a-z]/i.test(ch) ? 'teeth' : 'closed';
}
function setMouthPose(pose) {
  const shape = mouthShapes[pose] || mouthShapes.closed;
  const mouth = document.getElementById('mouth-open');
  const smile = document.getElementById('mouth-smile');
  if (!mouth || !smile) return;
  mouth.setAttribute('d', shape);
  mouth.setAttribute('opacity', pose === 'closed' ? '0' : '1');
  smile.setAttribute('opacity', pose === 'closed' ? '1' : '0');
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
      setMouthPose(mouthForChar(ch));
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
  setMouthPose('closed');
  setSpeaking(false);
  setAvatarStatus('Idle');
}
function speak(text, onWord) {
  if (document.getElementById('announce').checked && 'speechSynthesis' in window) {
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 0.85;
    utterance.onstart = () => { setSpeaking(true); setAvatarStatus('Speaking'); };
    utterance.onend = () => { setMouthPose('closed'); setSpeaking(false); setAvatarStatus('Idle'); };
    utterance.onboundary = (event) => {
      if (event.charIndex !== undefined) setMouthPose(mouthForChar(text[event.charIndex]));
    };
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
  if (localStorage.getItem('aidon-theme') === 'dark') { document.body.classList.add('dark'); }
  document.getElementById('theme-toggle').addEventListener('click', function() {
    var dark = document.body.classList.toggle('dark');
    try { localStorage.setItem('aidon-theme', dark ? 'dark' : 'light'); } catch (e) {}
  });
} catch (e) {}
</script>
</body></html>
"""

DAG_HTML = '<!DOCTYPE html>\n<html lang="en" data-theme="light">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>Aidon — DAG editor</title>\n<link rel="stylesheet" href="/static/style.css?v=__ASSET_V__">\n</head>\n<body class="dag-body">\n\n<header class="dag-top">\n  <a class="dag-brand" href="/">◈ Aidon</a>\n  <span class="dag-title">DAG editor</span>\n  <label class="dag-goal-label">Workflow goal <input id="dag-goal" type="text" placeholder="Describe when this workflow applies"></label>\n  <div class="dag-actions">\n    <button id="btn-new" class="dag-btn">New DAG</button>\n    <button id="btn-add" class="dag-btn">Add node</button>\n    <button id="btn-layout" class="dag-btn">Auto layout</button>\n    <button id="btn-fit" class="dag-btn">Fit graph</button>\n    <button id="btn-validate" class="dag-btn">Validate</button>\n    <button id="btn-export" class="dag-btn">Export JSON</button>\n    <button id="btn-save" class="dag-btn dag-btn-primary">Save</button>\n    <button id="btn-delete" class="dag-btn dag-btn-danger">Delete DAG</button>\n  </div>\n  <div class="dag-status" id="status">pick a graph on the left</div>\n</header>\n\n<main class="dag-main">\n  <!-- left: the saved graphs -->\n  <aside class="dag-side">\n    <input id="search" class="dag-search" type="text" placeholder="filter graphs…">\n    <div id="dag-list" class="dag-list"></div>\n  </aside>\n\n  <!-- center: the canvas -->\n  <section class="dag-canvas-wrap">\n    <svg id="canvas" class="dag-canvas">\n      <defs>\n        <pattern id="grid" width="28" height="28" patternUnits="userSpaceOnUse">\n          <path d="M 28 0 L 0 0 0 28" fill="none" stroke="var(--border)"\n                stroke-width="1" opacity="0.5"/>\n        </pattern>\n        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"\n                markerWidth="7" markerHeight="7" orient="auto-start-reverse">\n          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--accent)"/>\n        </marker>\n        <marker id="arrow-dim" viewBox="0 0 10 10" refX="9" refY="5"\n                markerWidth="7" markerHeight="7" orient="auto-start-reverse">\n          <path d="M 0 0 L 10 5 L 0 10 z" fill="var(--muted)"/>\n        </marker>\n      </defs>\n      <rect id="grid-bg" x="0" y="0" width="100%" height="100%"\n            fill="url(#grid)"/>\n      <g id="viewport">\n        <g id="edges"></g>\n        <g id="nodes"></g>\n        <path id="linking" class="dag-linking" style="display:none"/>\n      </g>\n    </svg>\n    <div class="dag-hint">drag a node · drag from its right port to another\n      node to connect · click the background to pan · wheel to zoom</div>\n  </section>\n\n  <!-- right: the node inspector -->\n  <aside class="dag-inspector">\n    <div id="inspector-empty" class="dag-empty">\n      select a node to edit it\n      <ul class="dag-empty-list">\n        <li><b>id</b> — stable key, used by templates</li>\n        <li><b>name</b> — human title on the canvas</li>\n        <li><b>tool</b> — the tool this step calls</li>\n        <li><b>args</b> — JSON passed to the tool</li>\n        <li><b>deps</b> — nodes that must finish first</li>\n        <li><b>executor</b> — tool / fast / strong (llm nodes)</li>\n      </ul>\n    </div>\n\n    <div id="inspector" hidden>\n      <div class="dag-insp-head">\n        <span id="insp-id" class="dag-insp-id"></span>\n        <button id="btn-dup" class="dag-btn dag-btn-small">Duplicate</button>\n        <button id="btn-delnode" class="dag-btn dag-btn-small dag-btn-danger">\n          Delete</button>\n      </div>\n\n      <label class="dag-field"><span>Step name</span><input id="f-name" type="text"></label>\n      <label class="dag-field"><span>Deliverable</span><input id="f-deliverable" type="text"></label>\n\n      <label class="dag-field">\n        <span>Tool</span>\n        <select id="f-tool"></select>\n      </label>\n\n      <label class="dag-field">\n        <span>Arguments (JSON)</span>\n        <textarea id="f-args" rows="7" spellcheck="false"></textarea>\n      </label>\n\n      <label class="dag-field"><span>Inputs (one per line)</span><textarea id="f-inputs" rows="3"></textarea></label>\n      <label class="dag-field"><span>Outputs (one per line)</span><textarea id="f-outputs" rows="3"></textarea></label>\n      <label class="dag-field"><span>Preconditions (one per line)</span><textarea id="f-preconditions" rows="3"></textarea></label>\n      <label class="dag-field"><span>Expected result</span><textarea id="f-expected" rows="3"></textarea></label>\n      <label class="dag-field"><span>Assumptions (one per line)</span><textarea id="f-assumptions" rows="3"></textarea></label>\n\n      <label class="dag-field">\n        <span>Executor</span>\n        <select id="f-executor">\n          <option value="tool">tool (deterministic)</option>\n          <option value="fast">fast model</option>\n          <option value="strong">strong model</option>\n        </select>\n      </label>\n\n      <div class="dag-field">\n        <span>Depends on</span>\n        <div id="f-deps" class="dag-deps"></div>\n      </div>\n\n      <div class="dag-insp-foot">\n        <span id="insp-state" class="dag-note"></span>\n      </div>\n    </div>\n  </aside>\n</main>\n\n<script src="/static/dag.js?v=__ASSET_V__"></script>\n</body>\n</html>\n'

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


def _api_key_row(key: dict) -> str:
    """One API-key row of the settings page, escaped: key names are owner-supplied text."""
    name = html.escape(str(key.get("name", "")))
    scope = html.escape(str(key.get("scope", "")))
    prefix = html.escape(str(key.get("key_hash_prefix", "")))
    return (f"<div class=\"set-row\"><div class=\"set-label\"><label>{name}</label>"
            f"<span class=\"set-key\">{scope}</span></div><div class=\"set-input\">"
            f"<span class=\"set-key\">hash {prefix}</span></div></div>")


class WebUIServer(WebUISettingsMixin, WebUIDataMixin, WebUICapabilityMixin):
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
        force_web = force_web or bool(body.get("force_web", False))
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
        lab_question = str(question).startswith("[AIDON_LAB] ")
        if lab_question:
            question = str(question)[len("[AIDON_LAB] "):]
        from .interview_store import InterviewStore
        journal = InterviewStore(self.home)
        turn_id = journal.question(run_id, conversation_id, str(question), lab_question)
        event = threading.Event()
        keys = [conversation_id] + [key for key in also_keys if key and key != conversation_id]
        waiter = {"run_id": run_id, "conversation_id": conversation_id,
                  "question": str(question), "event": event, "answer": None,
                  "keys": keys, "lab": lab_question, "turn_id": turn_id}
        with self._question_lock:
            self._question_waiters[run_id] = waiter
            for key in keys:
                self._question_by_conversation[key] = waiter
        record = self._web_runs.get(run_id)
        if record is not None:
            record["events"].append({"kind": "lab-question" if lab_question else "aidon", "text": str(question), "turn_id": turn_id})
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
            from .interview_store import InterviewStore
            InterviewStore(self.home).answer(waiter["turn_id"], str(answer))
            if waiter.get("lab"):
                record = self._web_runs.get(waiter["run_id"])
                if record is not None:
                    record["events"].append({"kind": "lab-answer", "text": str(answer), "turn_id": waiter["turn_id"]})
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

    def handle_skill_source(self, run_id: str, conversation_id: str) -> dict | None:
        """Expose only a successful workflow owned by this conversation."""
        run_id = str(run_id or "")
        if (not run_id or len(run_id) > 128
                or not run_id.replace("-", "").isalnum()):
            return None
        path = self.home / "learned" / "runs" / f"{run_id}.json"
        try:
            if path.stat().st_size > 1024 * 1024:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (record.get("status") != "done"
                or str(record.get("run_id", "")) != run_id
                or str(record.get("conversation_id", "")) != str(conversation_id)):
            return None
        steps = record.get("workflow")
        if not isinstance(steps, list) or not steps:
            return None
        return {"run_id": run_id, "conversation_id": str(conversation_id),
                "goal": str(record.get("goal", ""))[:4000],
                "deliverables": [str(x)[:300] for x in
                                 (record.get("deliverables") or [])[:100]],
                "workflow": [{
                    "id": str(node.get("id", ""))[:100],
                    "name": str(node.get("name", ""))[:300],
                    "tool": str(node.get("tool", ""))[:100],
                    "deliverable": str(node.get("deliverable", ""))[:300],
                    "depends_on": [str(x)[:100] for x in
                                   (node.get("depends_on") or [])[:100]],
                    "expected": str(node.get("expected", ""))[:1000],
                } for node in steps[:100] if isinstance(node, dict)]}

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
        raw_goal = goal
        conversation_id = str(body.get("conversation_id", "") or "")
        skill_source_run_id = str(body.get("skill_source_run_id", "") or "")
        if skill_source_run_id and not conversation_id:
            return {"error": "a verified skill source requires its original conversation"}
        if skill_source_run_id and not self.handle_skill_source(
                skill_source_run_id, conversation_id):
            return {"error": "skills can only be drafted from a verified successful run in this conversation"}
        conversation_db = None
        prior_history = []
        with self._question_lock:
            pending_waiter = self._question_by_conversation.get(conversation_id)
            answering_lab = bool(pending_waiter and pending_waiter.get("lab"))
        try:
            conversation_db = self._conversations_db()
            if not conversation_id:
                conversation_id = conversation_db.create_conversation()["id"]
            prior_history = conversation_db.history(conversation_id, limit=8)
            if not answering_lab:
                conversation_db.record_exchange(conversation_id, "user", raw_goal)
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
                    "goal": goal, "lab_answer": answering_lab}
        resume_interview = goal.strip().lower() == "/resume-interview"
        if resume_interview:
            from .interview_store import InterviewStore
            try:
                saved = InterviewStore(self.home).unfinished_for_conversation(conversation_id)
            except ValueError as exc:
                return self._simple_command_response(conversation_id, raw_goal, str(exc))
            if not saved:
                return self._simple_command_response(conversation_id, raw_goal, "There is no unfinished interview in this conversation.")
            goal = saved["goal"]
            raw_goal = goal
        if not resume_interview:
            goal = self._merge_correction_followup(raw_goal, prior_history)
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
        mode = "plan" if resume_interview else None
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
            mode = mode_enabled(self.home, conversation_id) or "plan"
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
                   "resume_interview": resume_interview,
                   "skill_source_run_id": skill_source_run_id,
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
        if record is None and control is not None:
            from .interview_store import InterviewStore
            saved_turns = InterviewStore(self.home).transcript(run_id)
            if saved_turns:
                out["status"] = "interrupted"
                out["events"] = []
                for turn in saved_turns:
                    out["events"].append({"kind": "lab-question" if turn["lab"] else "aidon",
                                          "text": turn["question"]})
                    if turn["answer"] is not None:
                        out["events"].append({"kind": "lab-answer" if turn["lab"] else "user",
                                              "text": turn["answer"]})
                out["events"].append({"kind": "aidon-final", "text":
                    "This interview was interrupted. Your saved answers are preserved. Send /resume-interview to resume it."})
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
        with self._question_lock:
            waiter = self._question_waiters.get(run_id)
            out["waiting_lab"] = bool(waiter and waiter.get("lab"))
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
                                       "mode": wa_mode or _wa_mode_enabled(self.home, wa_key) or "plan"}
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
            payload["mode"] = sl_mode or _sl_mode_enabled(self.home, sl_key) or "plan"
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
            list(body.get("nodes", [])), memory=self.memory,
            merge=bool(body.get("merge", False)),
            layout=body.get("layout", {}))
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


    def make_handler(self):
        from .webui_http import make_handler
        return make_handler(self)


    def serve(self, port: int = 8002) -> None:
        # Threading, so a goal run waiting on an approval card
        # does not block the /api/approve request that resolves it.
        ThreadingHTTPServer(("127.0.0.1", port), self.make_handler()).serve_forever()
