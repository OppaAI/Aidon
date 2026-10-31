"use strict";
// Capabilities page: renders GET /api/capabilities verbatim — every row
// comes from the live registry / skill library / approved-MCP file, so
// the page shows what Aidon can actually do, not a curated list.

try {
  document.documentElement.setAttribute(
    "data-theme", localStorage.getItem("aidon-theme") || "light");
} catch (e) { /* private mode: keep the default theme */ }

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function badge(text, cls) {
  return '<span class="cap-badge' + (cls ? " " + cls : "") + '">' +
    esc(text) + "</span>";
}

function card(name, desc, badges, code) {
  const attrs = code
    ? ' clickable" data-code-kind="' + esc(code.kind) +
      '" data-code-name="' + esc(code.name)
    : "";
  return '<div class="cap-card' + attrs + '"><div class="cap-name">' +
    esc(name) + '</div>' +
    (desc ? '<div class="cap-desc">' + esc(desc) + "</div>" : "") +
    (badges && badges.length
      ? '<div class="cap-meta">' + badges.join("") + "</div>" : "") +
    (code ? '<div class="cap-viewcode">view code →</div>' : "") +
    "</div>";
}

function empty(el, text) {
  el.innerHTML = '<p class="cap-empty">' + esc(text) + "</p>";
}

function skillCard(s) {
  const badges = [badge("v" + s.version, "")];
  if (s.tested) {
    badges.push(s.test_passed === true
      ? badge("tests passed", "ok")
      : badge("tests not passing", "bad"));
  } else {
    badges.push(badge("no tests recorded", "warn"));
  }
  if (s.failure_count > 0) {
    badges.push(badge(s.failure_count + " failed use(s)", "warn"));
  }
  if (s.privileged) badges.push(badge("privileged", "priv"));
  (s.capabilities || []).forEach(function (c) { badges.push(badge(c, "")); });
  return card(s.name, s.description, badges,
              { kind: "skill", name: s.name });
}

async function load() {
  let data;
  try {
    const r = await fetch("/api/capabilities");
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    document.getElementById("tools").innerHTML =
      '<p class="cap-empty">Could not load capabilities: ' +
      esc(e.message) + "</p>";
    return;
  }

  // The new stdlib server exposes compact lists; the old studio renders
  // richer records. Normalize both shapes so the restored UI remains useful.
  data.tools = (data.tools || []).map((t) => typeof t === "string"
    ? { name: t, description: "", origin: "core", side_effect: false } : t);
  data.counts = data.counts || { tools: data.tools.length };
  if (Array.isArray(data.skills)) {
    data.skills = { trusted: data.skills.map((name) =>
      ({ name, description: "", version: 1, tested: false })) };
  }
  data.skills = data.skills || {};
  data.mcp_servers = (data.mcp_servers || []).map((m) =>
    typeof m === "string" ? { name: m } : m);
  data.dags = data.dags || { count: 0, recent: [] };

  if (data.kernel_mode) {
    document.getElementById("kernel-banner").style.display = "block";
  }

  document.getElementById("tools-count").textContent =
    "(" + data.counts.tools + ")";
  const toolsEl = document.getElementById("tools");
  if (!data.tools.length) {
    empty(toolsEl, "No tools registered.");
  } else {
    toolsEl.innerHTML = data.tools.map(function (t) {
      const badges = [];
      if (t.origin === "plugin") badges.push(badge("adopted plugin", "plugin"));
      if (t.side_effect) badges.push(badge("acts on the world", "sidefx"));
      return card(t.name, t.description, badges,
                  { kind: "tool", name: t.name });
    }).join("");
  }

  const skills = data.skills || {};
  const verified = (skills.trusted || []).concat(skills.validated || []);
  const vEl = document.getElementById("skills-verified");
  if (!verified.length) {
    empty(vEl, "No verified skills yet — Aidon earns them by doing work.");
  } else {
    vEl.innerHTML = verified.map(function (s) {
      return skillCard(s);
    }).join("");
  }
  const cEl = document.getElementById("skills-candidate");
  if (!(skills.candidate || []).length) {
    empty(cEl, "No candidate skills in testing right now.");
  } else {
    cEl.innerHTML = skills.candidate.map(skillCard).join("");
  }
  const retired = skills.retired || [];
  const rWrap = document.getElementById("skills-retired-wrap");
  if (!retired.length) {
    rWrap.style.display = "none";
  } else {
    document.getElementById("skills-retired").innerHTML =
      retired.map(skillCard).join("");
  }

  const mcpEl = document.getElementById("mcp");
  if (!data.mcp_servers.length) {
    empty(mcpEl, "None connected — Aidon proposes one when a goal needs it.");
  } else {
    mcpEl.innerHTML = data.mcp_servers.map(function (m) {
      const badges = [];
      if (m.transport) badges.push(badge(m.transport, ""));
      if (m.target) badges.push(badge(m.target, ""));
      return card(m.name, "", badges);
    }).join("");
  }

  document.getElementById("dags-count").textContent =
    "(" + data.dags.count + ")";
  const dEl = document.getElementById("dags");
  if (!data.dags.recent.length) {
    empty(dEl, "No saved workflows yet.");
  } else {
    dEl.innerHTML = data.dags.recent.map(function (d) {
      return '<div class="cap-dagrow"><span class="cap-name">' +
        esc(d.name) + '</span><span class="cap-goal">' + esc(d.goal) +
        "</span></div>";
    }).join("");
  }
}

// ---- code viewer -----------------------------------------------------------
// Clicking a tool or skill card fetches the code behind it from
// /api/capabilities/code — a skill's stored content, a plugin tool's
// adopted file, a core tool's implementing function — and shows it in
// a modal. MCP-provided tools answer with an explanatory note instead
// of source; the modal shows the note.

const capModal = document.getElementById("cap-modal");
const capModalName = document.getElementById("cap-modal-name");
const capModalOrigin = document.getElementById("cap-modal-origin");
const capModalNote = document.getElementById("cap-modal-note");
const capModalCode = document.getElementById("cap-modal-code");

function closeCode() { capModal.hidden = true; }

async function openCode(kind, name) {
  capModalName.textContent = name;
  capModalOrigin.textContent = "";
  capModalNote.textContent = "";
  capModalCode.textContent = "Loading…";
  capModal.hidden = false;
  try {
    const r = await fetch("/api/capabilities/code?kind=" +
      encodeURIComponent(kind) + "&name=" + encodeURIComponent(name));
    if (!r.ok) throw new Error("HTTP " + r.status);
    const d = await r.json();
    capModalOrigin.textContent = d.origin || "";
    capModalNote.textContent = d.note || "";
    capModalCode.textContent = d.source != null || d.code != null
      ? (d.source != null ? d.source : d.code)
      : "(no local source — " + (d.note || "unavailable") + ")";
  } catch (e) {
    capModalCode.textContent = "Could not load the code: " + e.message;
  }
}

document.addEventListener("click", function (e) {
  const el = e.target.closest("[data-code-name]");
  if (el) openCode(el.getAttribute("data-code-kind"),
                   el.getAttribute("data-code-name"));
});
document.getElementById("cap-modal-close")
  .addEventListener("click", closeCode);
capModal.addEventListener("click", function (e) {
  if (e.target === capModal) closeCode();
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && !capModal.hidden) closeCode();
});

load();
