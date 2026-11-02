"""HTML templates for the WebUI and standalone workflow views."""

from __future__ import annotations

from pathlib import Path

from .webui_style import BASE_CSS


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

# Keep the standalone editor page in sync with its tracked HTML template.
DAG_HTML = (Path(__file__).parent / "static" / "dag.html").read_text(
    encoding="utf-8")

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



# Inject the shared style into the full-page constants defined above.
CHAT_HTML = CHAT_HTML.replace("</style></head>", "</style><style>" + BASE_CSS + "</style></head>", 1)
DAG_HTML = DAG_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
REPLAY_HTML = REPLAY_HTML.replace("</head>", "<style>" + BASE_CSS + "</style></head>", 1)
VRM_HTML = VRM_HTML.replace("</head>", "<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head>", 1)
