"use strict";

const KINDS = ["tool", "skill", "mcp"];
const state = {};
const revisions = {};
const busy = {};
const params = new URLSearchParams(location.search);
function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}
function fresh() {
  return { id: crypto.randomUUID().replace(/-/g, ""), turns: [], brief: null, options: [] };
}
function cache(kind) {
  localStorage.setItem("aidon-lab-" + kind, JSON.stringify(state[kind]));
}
function safeLink(url, label) {
  try {
    const parsed = new URL(url);
    if (parsed.protocol === "https:" || parsed.protocol === "http:")
      return '<a href="' + esc(parsed.href) + '" target="_blank" rel="noopener noreferrer">' + esc(label) + '</a>';
  } catch (_) { /* no link for an invalid address */ }
  return esc(label);
}
function render(kind) {
  const current = state[kind] || fresh();
  const box = document.getElementById("chat-" + kind);
  box.innerHTML = current.turns.map(turn =>
    '<div class="lab-msg ' + (turn.role === "user" ? "user" : "assistant") + '">' + esc(turn.text) + '</div>').join("");
  if (current.brief) {
    const brief = document.createElement("details");
    brief.className = "lab-brief";
    brief.open = true;
    const summary = document.createElement("summary");
    summary.textContent = "Brief to approve";
    const pre = document.createElement("pre");
    pre.textContent = JSON.stringify(current.brief, null, 2);
    brief.append(summary, pre);
    box.appendChild(brief);
  }
  box.scrollTop = box.scrollHeight;
  const options = document.getElementById("mcp-options");
  if (kind === "mcp") {
    options.innerHTML = (current.options || []).map(option => {
      const connection = option.connection;
      const address = connection ? (connection.url || [connection.command, ...(connection.args || [])].join(" ")) : "Connection needs additional setup";
      return '<article class="mcp-option"><strong>' + esc(option.title) + '</strong>'
        + '<p>' + esc(option.description) + '</p><p>Publisher: ' + esc(option.author)
        + ' · Version: ' + esc(option.version) + '</p><code>' + esc(address) + '</code><p>'
        + 'License: ' + esc(option.license) + ' · GitHub stars: ' + esc(option.stars == null ? "Unavailable" : option.stars)
        + '</p><p>Pricing: ' + esc(option.pricing) + '</p><p>Reviews: ' + esc(option.reviews) + '</p><p>'
        + safeLink(option.source, "Registry record") + ' · ' + safeLink(option.repository, "Repository")
        + (option.website ? ' · ' + safeLink(option.website, "Publisher website") : '') + '</p>'
        + '<button data-choice="' + esc(option.id) + '"' + ((!connection || busy.mcp) ? ' disabled' : '') + '>Use this server</button></article>';
    }).join("");
  }
  document.querySelector('[data-brief="' + kind + '"]').disabled = busy[kind] || !current.brief || Boolean(current.build);
  document.querySelector('[data-send="' + kind + '"]').disabled = Boolean(busy[kind]);
  document.querySelector('[data-reset="' + kind + '"]').disabled = Boolean(busy[kind]);
  if (current.build) showBuild(kind, current.build);
}
function showBuild(kind, result) {
  const status = document.getElementById("status-" + kind);
  const url = result.url || ("/?conversation_id=" + encodeURIComponent(result.conversation_id) + "&run_id=" + encodeURIComponent(result.run_id));
  status.innerHTML = 'Workflow started. <a href="' + esc(url) + '" target="_blank" rel="noopener">Open this workflow and its approvals →</a>';
}
async function post(url, body) {
  const response = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "HTTP " + response.status);
  return data;
}
async function send(kind, choice="") {
  if (busy[kind]) return;
  const input = document.getElementById("input-" + kind);
  const option = choice && (state[kind].options || []).find(x => x.id === choice);
  const message = option ? "Use " + option.title + " for the workflow we discussed." : input.value.trim();
  if (!message) return;
  const status = document.getElementById("status-" + kind);
  busy[kind] = true;
  render(kind);
  status.textContent = kind === "mcp" && !choice ? "Searching published MCP servers and official sources…" : "Aidon is preparing the next question or brief…";
  try {
    const data = await post("/api/labs/message", {
      kind, message, choice, conversation_id: state[kind].id,
      revision: revisions[kind], research: document.getElementById("research-" + kind).checked
    });
    state[kind] = data.state;
    revisions[kind] = data.revision;
    cache(kind);
    if (!choice) input.value = "";
    status.textContent = (data.warnings || []).join(" ");
  } catch (error) {
    status.textContent = error.message;
  } finally { busy[kind] = false; render(kind); }
}
async function startFromBrief(kind) {
  if (busy[kind] || !state[kind].brief) return;
  busy[kind] = true;
  render(kind);
  const status = document.getElementById("status-" + kind);
  status.textContent = "Starting the approved brief…";
  try {
    const data = await post("/api/labs/start", { kind, conversation_id: state[kind].id, revision: revisions[kind] });
    revisions[kind] = data.revision || revisions[kind];
    state[kind].build = data;
    cache(kind);
    showBuild(kind, data);
  } catch (error) { status.textContent = error.message; }
  finally { busy[kind] = false; render(kind); }
}
document.querySelectorAll(".lab-tab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".lab-tab").forEach(t => t.setAttribute("aria-selected", String(t === tab)));
  document.querySelectorAll(".lab-panel").forEach(p => p.classList.toggle("active", p.id === "lab-" + tab.dataset.lab));
}));
document.querySelectorAll("[data-send]").forEach(button => button.addEventListener("click", () => send(button.dataset.send)));
document.querySelectorAll("[data-brief]").forEach(button => button.addEventListener("click", () => startFromBrief(button.dataset.brief)));
document.getElementById("mcp-options").addEventListener("click", event => {
  const button = event.target.closest("[data-choice]");
  if (button) send("mcp", button.dataset.choice);
});
document.querySelectorAll("[data-reset]").forEach(button => button.addEventListener("click", () => {
  const kind = button.dataset.reset;
  if (busy[kind]) return;
  state[kind] = fresh(); revisions[kind] = 0; cache(kind); render(kind);
  document.getElementById("status-" + kind).textContent = "New interview started.";
}));
async function initialize(kind) {
  busy[kind] = true;
  try { state[kind] = JSON.parse(localStorage.getItem("aidon-lab-" + kind) || "null") || fresh(); }
  catch (_) { state[kind] = fresh(); }
  revisions[kind] = 0;
  render(kind);
  try {
    const response = await fetch("/api/lab-session?kind=" + kind + "&conversation_id=" + encodeURIComponent(state[kind].id));
    const saved = await response.json();
    if (!response.ok) throw new Error(saved.error || "Could not load the lab");
    if (saved.state) state[kind] = saved.state;
    revisions[kind] = saved.revision;
    if (kind === "skill" && params.get("source_run")) {
      const sourceResponse = await fetch("/api/skill-source?run_id=" + encodeURIComponent(params.get("source_run"))
        + "&conversation_id=" + encodeURIComponent(params.get("conversation_id") || ""));
      const source = await sourceResponse.json();
      if (!sourceResponse.ok) throw new Error(source.error || "Could not load the workflow source");
      if (state.skill.source_run_id !== source.run_id) {
        state.skill = fresh(); revisions.skill = 0;
        state.skill.source_run_id = source.run_id;
        state.skill.turns.push({ role: "user", text: "Distill this verified workflow into a reusable skill:\n" + JSON.stringify(source) });
        const savedSource = await post("/api/lab-session", {kind, conversation_id: state.skill.id, state: state.skill, revision: 0});
        revisions.skill = savedSource.revision;
      }
    }
    cache(kind);
    busy[kind] = false;
    render(kind);
  } catch (error) { document.getElementById("status-" + kind).textContent = error.message; }
  document.getElementById("input-" + kind).addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(kind); }
  });
}
KINDS.forEach(initialize);
const selectedTab = params.get("lab") || (params.has("source_run") ? "skill" : "tool");
if (KINDS.includes(selectedTab)) document.querySelector('[data-lab="' + selectedTab + '"]').click();
