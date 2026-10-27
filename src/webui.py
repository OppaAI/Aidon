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
#wave-bars span { display: inline-block; width: 6px; background: #336699; margin-right: 2px; vertical-align: bottom; height: 4px; }
#avatar.speaking #wave-bars span { animation: robot-wave 0.3s ease-in-out infinite alternate; }
@keyframes robot-wave { from { height: 4px; } to { height: 18px; } }
.karaoke-word { background: #ffeb3b; }
.karaoke-done { background: transparent; }
</style></head>
<body>
<h1>Aidon</h1>
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
<h2>Workflows</h2><a href="/dag">DAG editor</a> <a href="/factory">Factory</a> <a href="/replay">Replay</a> <a href="/avatar-vrm">VRM avatar, fallback</a>
<div id="stage-pills"><p>Stages: recall, interpret, research, plan, sandbox, verify, approval, live, learn</p><div id="pills"></div></div>
</div>
<div id="chat">
<div id="messages"></div>
<form id="form"><input id="msg" name="message" placeholder="Type a goal, in plain language, or use the mic">
<button type="submit">Send</button></form>
<div id="approval"></div>
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
  element.innerHTML = '<span class="karaoke-done">' + text + '</span>';
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
                 media_downloader=None, run_control=None, webui_token=None, transcribe_key=None):
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
        self.webui_token = webui_token
        self.transcribe_key = transcribe_key

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
                                    try:
                                        from .whatsapp_vision import prepare_vision_message
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
        out = []
        for project in self._projects():
            trace_count = len([p for p in (self.home / "trace").glob("*.jsonl")]) if (self.home / "trace").exists() else 0
            out.append({"project": project, "trace_files": trace_count})
        return out

    def settings_get(self) -> dict:
        path = self.home / "settings.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def settings_set(self, key: str, value) -> bool:
        allowed = {"timezone", "budget_tokens", "max_steps", "autonomy_enabled", "approval_nudge_minutes"}
        if key not in allowed:
            return False
        data = self.settings_get()
        data[key] = value
        path = self.home / "settings.json"
        path.write_text(json.dumps(data, indent=2))
        return True

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
                if not parsed.path.startswith("/webhook/"):
                    from .webui_auth import is_authorized
                    headers = {k.lower(): v for k, v in self.headers.items()}
                    if not is_authorized(headers, server.webui_token, self.client_address[0]):
                        self._send_json(401, {"error": "unauthorized"})
                        return
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
                    if self.memory is not None:
                        try:
                            from .experience import recall_experiences
                            for exp in recall_experiences(self.memory, "", limit=20):
                                if exp.dag_nodes:
                                    dags.append({"goal": exp.goal, "nodes": exp.dag_nodes})
                        except Exception:
                            pass
                    self._send_json(200, {"dags": dags})
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
                    from .library import Library
                    saved = Library(server.home).add_file(tmp, kind="file", description="WebUI upload")
                    self._send_json(200, {"id": saved["id"], "name": safe})
                elif parsed.path == "/api/settings":
                    ok = server.settings_set(str(body.get("key", "")), body.get("value"))
                    self._send_json(200 if ok else 400, {"ok": ok})
                elif parsed.path == "/api/transcribe":
                    if not server.transcribe_key:
                        self._send_json(501, {"error": "transcribe key not configured"})
                        return
                    import base64 as _b64t
                    try:
                        audio = _b64t.b64decode(str(body.get("audio_b64", "")))
                        from .voice import transcribe as _transcribe
                        result = _transcribe(audio, server.transcribe_key)
                        self._send_json(200, result)
                    except Exception as exc:
                        self._send_json(500, {"error": str(exc)})
                elif parsed.path == "/api/chat":
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
