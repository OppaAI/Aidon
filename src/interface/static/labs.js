"use strict";

try {
  document.documentElement.setAttribute("data-theme", localStorage.getItem("aidon-theme") || "light");
} catch (_) { /* keep theme default */ }

const LABS = {
  tool: {
    title: "Tool Crafting Lab",
    intro: "Interview me step by step to define a tool I want Aidon to create. Clarify the actual task and expected result, input/output, whether this should use an AI model or a named software/service, credentials and access boundaries, where it should run, and how to test it. For image editing, first ask whether I want an AI image model (ask which one) or editing software (ask which software); do not choose silently. Ask one concise question at a time and wait for my answer. If I request suggestions or online research, compare concrete options and distinguish free, paid, and open-source choices with sources. Do not write or adopt anything during this interview. When enough is known, give a clearly labeled TOOL SPEC with purpose, interface, dependencies, capabilities, safety boundaries, and acceptance tests, then ask me to approve or revise it."
  },
  skill: {
    title: "Skill Learning Lab",
    intro: "Interview me step by step about a repeatable workflow I want Aidon to learn. Clarify when to use it, expected outcome, my preferred process, examples, exceptions, safety boundaries, and how success is checked. Ask one concise question at a time. Research conventions or sources only if I ask. Do not create or import a skill during this interview. When enough is known, give a clearly labeled SKILL SPEC and a Claude-style SKILL.md draft: YAML front matter with name and description, concise instructions, and a ## Tests section with safe, repeatable checks required by Aidon's library. Ask me to approve or revise it."
  },
  mcp: {
    title: "MCP Connection Lab",
    intro: "Interview me step by step about what external workflow or service I want Aidon to use. Clarify the task, data involved, account/region constraints, and whether I prefer local/open-source, free, or paid options. Ask one concise question at a time. Research concrete MCP servers and compare fit, pricing, open-source status, publisher/source, and exact connection method; cite sources and never invent a server command or URL. Do not connect anything during the interview. When enough is known, produce a clearly labeled MCP CONNECTION SPEC with the chosen server, exact stdio command+args or HTTP URL, environment variable names (never secret values), requested tools, permissions, and approval risks. Ask me to approve or revise it."
  }
};

function esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, ch =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch]);
}
function newConversationId() {
  if (window.crypto && window.crypto.randomUUID) {
    return window.crypto.randomUUID().replace(/-/g, "");
  }
  return Array.from({ length: 32 }, () =>
    Math.floor(Math.random() * 16).toString(16)).join("");
}
function stateKey(kind) { return "aidon-lab-" + kind; }
function readState(kind) {
  try {
    const value = JSON.parse(localStorage.getItem(stateKey(kind)) || "null");
    if (value && Array.isArray(value.turns)) return { created: false, ...value };
  } catch (_) { /* start a fresh session */ }
  return { id: "", created: false, started: false, turns: [] };
}
const state = Object.fromEntries(Object.keys(LABS).map(k => [k, readState(k)]));
const revisions = { tool: 0, skill: 0, mcp: 0 };

function render(kind) {
  const box = document.getElementById("chat-" + kind);
  box.innerHTML = state[kind].turns.map(turn =>
    '<div class="lab-msg ' + (turn.role === "user" ? "user" : "assistant") + '">' + esc(turn.text) + "</div>").join("");
  box.scrollTop = box.scrollHeight;
  const marker = { tool: /TOOL SPEC/i, skill: /SKILL SPEC/i,
                   mcp: /MCP CONNECTION SPEC/i }[kind];
  const ready = state[kind].turns.some(turn => turn.role === "assistant" && marker.test(turn.text));
  document.querySelector('[data-brief="' + kind + '"]').disabled = !ready;
}
async function save(kind) {
  localStorage.setItem(stateKey(kind), JSON.stringify(state[kind]));
  const response = await fetch("/api/lab-session", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, conversation_id: state[kind].id,
      state: state[kind], revision: revisions[kind] })
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Could not save the lab session");
  revisions[kind] = data.revision;
}

async function send(kind) {
  const input = document.getElementById("input-" + kind);
  const status = document.getElementById("status-" + kind);
  const button = document.querySelector('[data-send="' + kind + '"]');
  const text = input.value.trim();
  if (!text) return;
  const research = document.getElementById("research-" + kind).checked;
  state[kind].turns.push({ role: "user", text });
  if (!state[kind].id) state[kind].id = newConversationId();
  render(kind); input.value = ""; button.disabled = true;
  status.textContent = "Aidon is thinking…";
  const message = (state[kind].started ? "" : "[" + LABS[kind].title + "] " + LABS[kind].intro + "\n\n") + text;
  try {
    await save(kind);
    if (!state[kind].created) {
      const created = await fetch("/api/conversations", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: state[kind].id })
      });
      const record = await created.json();
      if (!created.ok || !record.id) throw new Error(record.error || "Could not create lab conversation");
      state[kind].id = record.id;
      state[kind].created = true;
      await save(kind);
    }
    const response = await fetch("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: state[kind].id, force_web: research })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
    const reply = data.reply || "Aidon did not return a response.";
    state[kind].started = true;
    state[kind].turns.push({ role: "assistant", text: reply });
    await save(kind); render(kind); status.textContent = "";
  } catch (error) {
    status.textContent = "Could not reach Aidon: " + error.message;
  } finally { button.disabled = false; }
}

function startFromBrief(kind) {
  const turns = state[kind].turns;
  if (!turns.length) {
    document.getElementById("status-" + kind).textContent = "Tell Aidon what you want first.";
    return;
  }
  const marker = { tool: /TOOL SPEC/i, skill: /SKILL SPEC/i,
                   mcp: /MCP CONNECTION SPEC/i }[kind];
  if (!turns.some(turn => turn.role === "assistant" && marker.test(turn.text))) {
    document.getElementById("status-" + kind).textContent = "Finish the interview and review Aidon's spec before approving the brief.";
    return;
  }
  const transcript = turns.map(t => (t.role === "user" ? "Owner" : "Aidon") + ":\n" + t.text).join("\n\n");
  const instruction = {
    tool: "Use the conversation below as the owner's approved requirements brief. First summarize the agreed TOOL SPEC and identify any remaining ambiguity; if the brief is sufficient, create the proposed sandbox tool plugin in the goal workdir, test it safely, and request adoption through the existing explicit plugin approval card. Do not assume brief approval is plugin adoption approval. For image work, follow the user's chosen model/software.\n\n",
    skill: "Use the conversation below as the owner's approved requirements brief. Produce a reusable Claude-style SKILL.md with YAML front matter name and description, concise actionable instructions, and a ## Tests section with meaningful safe checks. Import it through the existing SkillLibrary path, run the skill tests if the workflow supports it, and request any required trust approvals. Do not claim it is trusted until lifecycle checks pass and owner approval is recorded.\n\n",
    mcp: "Use the conversation below as the owner's approved MCP requirements brief. Research any missing details, use only a real, cited server and exact command or URL, then request connection through the existing MCP approval lifecycle. Never invent a command or URL and never ask the owner to paste a secret into chat; use named environment variables. Do not connect before the approval card is accepted.\n\n"
  }[kind];
  const goal = instruction + transcript;
  const status = document.getElementById("status-" + kind);
  status.textContent = "Starting the approved brief in a new workflow…";
  fetch("/api/goals", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ goal, mode: "plan", conversation_id: state[kind].id }) })
    .then(async response => {
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "HTTP " + response.status);
      status.innerHTML = 'Workflow started. Follow it in <a href="/">Chat</a>; required adoption or connection approvals will appear there.';
    })
    .catch(error => { status.textContent = "Could not start workflow: " + error.message; });
}

document.querySelectorAll(".lab-tab").forEach(tab => tab.addEventListener("click", () => {
  document.querySelectorAll(".lab-tab").forEach(t => t.setAttribute("aria-selected", String(t === tab)));
  document.querySelectorAll(".lab-panel").forEach(p => p.classList.toggle("active", p.id === "lab-" + tab.dataset.lab));
}));
document.querySelectorAll("[data-send]").forEach(btn => btn.addEventListener("click", () => send(btn.dataset.send)));
document.querySelectorAll("[data-brief]").forEach(btn => btn.addEventListener("click", () => startFromBrief(btn.dataset.brief)));
document.querySelectorAll("[data-reset]").forEach(btn => btn.addEventListener("click", async () => {
  const kind = btn.dataset.reset;
  state[kind] = { id: newConversationId(), created: false,
    started: false, turns: [] };
  revisions[kind] = 0;
  try {
    await save(kind); render(kind); document.getElementById("status-" + kind).textContent = "New interview started.";
  } catch (error) { document.getElementById("status-" + kind).textContent = error.message; }
}));
Object.keys(LABS).forEach(async kind => {
  const sendButton = document.querySelector('[data-send="' + kind + '"]');
  sendButton.disabled = true;
  try {
    const response = await fetch("/api/lab-session?kind=" + encodeURIComponent(kind)
      + "&conversation_id=" + encodeURIComponent(state[kind].id || ""));
    if (!response.ok) throw new Error("Could not load saved lab session");
    const saved = await response.json();
    revisions[kind] = saved.revision;
    if (saved.state && Array.isArray(saved.state.turns)) state[kind] = saved.state;
  } catch (error) {
    document.getElementById("status-" + kind).textContent = error.message;
    return;
  }
  sendButton.disabled = false;
  render(kind);
  document.getElementById("input-" + kind).addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); send(kind); }
  });
});
