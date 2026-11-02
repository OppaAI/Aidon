"use strict";

try { document.documentElement.setAttribute("data-theme", localStorage.getItem("aidon-theme") || "light"); } catch (_) {}
function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]); }
function badge(text, cls="") { return '<span class="cap-badge ' + esc(cls) + '">' + esc(text) + '</span>'; }
function icon(text, kind) {
  const s = String(text || "").toLowerCase();
  if (/todo|task|project|checklist/.test(s)) return "✅";
  if (/calendar|schedule|cron|timer/.test(s)) return "🗓️";
  if (/note|wiki|knowledge|memory/.test(s)) return "📝";
  if (/image|photo|draw|segment/.test(s)) return "🖼️";
  if (/slack|whatsapp|email|message|notify|publish|social/.test(s)) return "💬";
  if (/search|web|fetch|research|youtube/.test(s)) return "🔎";
  if (/file|document|pdf|ebook|spreadsheet|slide|docx/.test(s)) return "📄";
  if (/audio|voice|speech|transcri|record/.test(s)) return "🎙️";
  if (/install|package|apt|venv/.test(s)) return "📦";
  if (/shell|python|code|plugin|tool/.test(s)) return "🧰";
  if (kind === "mcp") return "🔌";
  if (kind === "skill") return "📘";
  return "✨";
}
function tab(category) {
  document.querySelectorAll(".cap-tab").forEach(t => t.setAttribute("aria-selected", String(t.dataset.capTab === category)));
  document.querySelectorAll(".cap-panel").forEach(p => p.classList.toggle("active", p.id === "cap-" + category));
}
document.querySelectorAll(".cap-tab").forEach(t => t.addEventListener("click", () => tab(t.dataset.capTab)));
function renderList(element, items, kind, label) {
  if (!items.length) { element.innerHTML = '<p class="cap-empty">' + esc(label) + '</p>'; return; }
  element.innerHTML = items.map((item, i) => '<button type="button" data-kind="' + esc(kind) + '" data-index="' + i + '" aria-selected="false"><span class="cap-list-name">' + icon(item.name, kind) + ' ' + esc(item.name) + '</span><span class="cap-list-sub">' + esc(item.description || item.subtitle || item.origin || item.status || "") + '</span></button>').join("");
}
async function showDetails(kind, item, detail, button) {
  const list = button.parentElement;
  list.querySelectorAll("button").forEach(b => b.setAttribute("aria-selected", String(b === button)));
  detail.innerHTML = '<p>Loading details…</p>';
  let source = null;
  try {
    const response = await fetch("/api/capabilities/code?kind=" + encodeURIComponent(kind) + "&name=" + encodeURIComponent(item.name));
    if (response.ok) source = await response.json();
  } catch (_) {}
  const desc = item.description || item.goal || "";
  const meta = [];
  if (item.origin) meta.push(badge(item.origin, item.origin === "plugin" ? "plugin" : ""));
  if (item.state || item.status) meta.push(badge(item.state || item.status, item.state === "trusted" || item.status === "trusted" ? "ok" : ""));
  if (item.version) meta.push(badge("v" + item.version));
  if (item.tested) meta.push(badge(item.test_passed === true ? "tests passed" : "tests recorded", item.test_passed === true ? "ok" : "warn"));
  if (item.side_effect) meta.push(badge("approval gated", "sidefx"));
  if (item.transport) meta.push(badge(item.transport));
  const heading = '<div class="cap-detail-head">' + meta.join("") + '</div><h2>' + icon(item.name, kind) + ' ' + esc(item.name) + '</h2>';
  let info = '<p>' + esc(desc || (kind === "tool" ? "Available in the current tool registry." : kind === "skill" ? "Saved reusable instructions." : "Connected MCP service.")) + '</p>';
  if (item.capabilities && item.capabilities.length) info += '<p>Capabilities: ' + esc(item.capabilities.join(", ")) + '</p>';
  if (kind === "tool") info += '<p>' + (item.side_effect ? "Some actions require approval before they affect external systems." : "This tool is available to Aidon under the current configuration.") + '</p>';
  if (kind === "skill") info += '<p>Skills are reusable instructions. Their state shows whether they are still candidates, validated, trusted, or retired.</p>';
  if (kind === "mcp") {
    const cfg = item.config || {};
    if (cfg.url) info += '<p>Address: ' + esc(cfg.url) + '</p>';
    if (cfg.command) info += '<p>Command: ' + esc([cfg.command].concat(cfg.args || []).join(" ")) + '</p>';
    if (cfg.tools && cfg.tools.length) info += '<p>Tools: ' + esc(cfg.tools.join(", ")) + '</p>';
    if (cfg.environment_variables && cfg.environment_variables.length) info += '<p>Environment variables: ' + esc(cfg.environment_variables.join(", ")) + ' (values hidden)</p>';
    if (cfg.authentication) info += '<p>' + esc(cfg.authentication) + '</p>';
  }
  const code = source && (source.source != null || source.code != null) ? (source.source != null ? source.source : source.code) : "";
  const note = source && source.note ? '<p>' + esc(source.note) + '</p>' : "";
  detail.innerHTML = heading + info + note + (code ? '<h3 class="cap-code-title">' + (kind === "skill" ? "Skill Markdown" : kind === "mcp" ? "Safe connection record" : "Source") + '</h3><pre class="cap-code">' + esc(code) + '</pre>' : (kind === "mcp" ? "" : '<p class="cap-empty">' + esc(source ? "Source is not available for this item." : "Could not load source details.") + '</p>'));
}
function activateFirst(kind, list, items, detail) {
  list.onclick = e => {
    const button = e.target.closest("button[data-index]");
    if (button) showDetails(kind, items[Number(button.dataset.index)], detail, button);
  };
  if (items.length) showDetails(kind, items[0], detail, list.querySelector("button"));
}
async function load() {
  let data;
  try { const r = await fetch("/api/capabilities"); if (!r.ok) throw new Error("HTTP " + r.status); data = await r.json(); }
  catch (e) { document.getElementById("tools-list").innerHTML = '<p class="cap-empty">Could not load capabilities: ' + esc(e.message) + '</p>'; return; }
  const tools = (data.tools || []).map(t => typeof t === "string" ? {name:t} : t);
  const skills = (data.skills || []).map(s => typeof s === "string" ? {name:s} : s).sort((a,b) => String(a.name).localeCompare(String(b.name)));
  const mcps = (data.mcp_servers || []).map(m => typeof m === "string" ? {name:m} : m).sort((a,b) => String(a.name).localeCompare(String(b.name)));
  const retired = skills.filter(s => s.state === "retired");
  const liveSkills = skills.filter(s => s.state !== "retired");
  if (data.kernel_mode) document.getElementById("kernel-banner").style.display = "block";
  const lists = [["tools",tools,"tools-list","tools-detail","No tools registered."],["skill",liveSkills,"skills-list","skills-detail","No saved skills yet."],["mcp",mcps,"mcp-list","mcp-detail","No connected MCP servers yet."]];
  lists.forEach(([kind,items,listId,detailId,emptyText]) => { const list=document.getElementById(listId); renderList(list,items,kind,emptyText); activateFirst(kind,list,items,document.getElementById(detailId)); });
  if (retired.length) {
    const list = document.getElementById("skills-list");
    const divider = document.createElement("p"); divider.className="cap-empty"; divider.textContent="Retired"; list.appendChild(divider);
    retired.forEach(skill => { const button=document.createElement("button"); button.type="button"; button.dataset.kind="skill"; button.dataset.index=String(skills.indexOf(skill)); button.innerHTML='<span class="cap-list-name">'+icon(skill.name,"skill")+' '+esc(skill.name)+'</span><span class="cap-list-sub">retired</span>'; button.addEventListener("click",event=>{event.stopPropagation(); showDetails("skill",skill,document.getElementById("skills-detail"),button);}); list.appendChild(button); });
  }
  const dags=data.dags||{count:0,recent:[]};
  document.getElementById("dags-count").textContent="("+(dags.count||0)+")";
  const d=document.getElementById("dags");
  d.innerHTML=(dags.recent||[]).length ? dags.recent.map(x=>'<div class="cap-dagrow"><span class="cap-list-name">🔀 '+esc(x.name)+'</span><span class="cap-goal">'+esc(x.goal)+'</span></div>').join("") : '<p class="cap-empty">No saved workflows yet.</p>';
}
load();
