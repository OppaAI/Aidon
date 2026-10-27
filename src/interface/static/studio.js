"use strict";
// Memory / Knowledge Studio pages: render one Qdrant collection from
// /api/memory/<collection> — every card is a stored point, shown from
// its payload, with a per-item delete. The page body carries the
// config: data-api (list endpoint), data-kind ("episode"|"knowledge").

try {
  document.documentElement.setAttribute(
    "data-theme", localStorage.getItem("aidon-theme") || "dark");
} catch (e) { /* private mode: keep the default theme */ }

const API = document.body.dataset.api;
const KIND = document.body.dataset.kind;
const PAGE = 50;
let offset = 0;
let matched = 0;

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function badge(text, cls) {
  return '<span class="cap-badge' + (cls ? " " + cls : "") + '">' +
    esc(text) + "</span>";
}

function fmtDate(s) {
  return s ? String(s).slice(0, 10) : "";
}

function field(label, text) {
  if (!text) return "";
  return '<div class="studio-field"><div class="studio-label">' +
    esc(label) + '</div><div class="studio-text">' + esc(text) +
    "</div></div>";
}

function card(item) {
  const badges = [];
  if (item.created_at) badges.push(badge(fmtDate(item.created_at), ""));
  if (item.access_count) {
    badges.push(badge("recalled ×" + item.access_count, "ok"));
  }
  let title, body = "";
  if (KIND === "episode") {
    title = item.goal || "(no goal recorded)";
    if (item.conversation_id) badges.push(badge("from a chat", ""));
    body = field("Outcome", item.outcome) +
           field("Lesson", item.lesson) +
           field("Approach", item.approach);
  } else {
    title = item.title || item.doc_id || "(untitled)";
    if (item.kind) badges.push(badge(item.kind, "plugin"));
    if (item.doc_id) badges.push(badge(item.doc_id, ""));
    body = field("", item.text);
  }
  return '<div class="cap-card"><button class="studio-del" ' +
    'data-del="' + esc(item.id) + '" title="Delete this ' + KIND +
    '">✕</button><div class="cap-name">' + esc(title) + "</div>" +
    '<div class="cap-meta">' + badges.join("") + "</div>" + body +
    "</div>";
}

async function loadPage(append) {
  const q = document.getElementById("studio-q").value.trim();
  const list = document.getElementById("studio-list");
  const more = document.getElementById("studio-more");
  if (!append) { offset = 0; list.innerHTML = ""; }
  let data;
  try {
    const r = await fetch(API + "?limit=" + PAGE + "&offset=" + offset +
      (q ? "&q=" + encodeURIComponent(q) : ""));
    if (!r.ok) throw new Error("HTTP " + r.status);
    data = await r.json();
  } catch (e) {
    list.innerHTML = '<p class="cap-empty">Could not load: ' +
      esc(e.message) + "</p>";
    return;
  }
  document.getElementById("studio-count").textContent =
    "(" + data.total + ")";
  document.getElementById("studio-mode").textContent = data.mode
    ? "Memory store: " + data.mode : "";
  const err = document.getElementById("studio-error");
  if (data.error) {
    err.textContent = "The memory store could not be read: " + data.error;
    err.style.display = "block";
  } else {
    err.style.display = "none";
  }
  const rawItems = Array.isArray(data.items) ? data.items :
    (data.points || []).map((p) => Object.assign({}, p.payload || {}, {
      id: p.id, text: p.text || (p.payload || {}).text || "",
    }));
  const items = rawItems;
  const total = data.total != null ? data.total : items.length;
  document.getElementById("studio-count").textContent = "(" + total + ")";
  matched = data.matched != null ? data.matched : items.length;
  if (!append && !items.length) {
    list.innerHTML = '<p class="cap-empty">' +
      (q ? "Nothing matches that search."
          : "Nothing stored yet — Aidon adds to this as it works.") +
      "</p>";
  } else {
    list.insertAdjacentHTML("beforeend",
      items.map(card).join(""));
  }
  offset += items.length;
  more.style.display = offset < matched ? "block" : "none";
  if (data.capped) {
    document.getElementById("studio-mode").textContent +=
      " · showing the first pages of a very large store";
  }
}

async function del(id) {
  if (!window.confirm("Delete this " + KIND + "? Aidon will no " +
      "longer recall it.")) return;
  try {
    const r = await fetch(API + "/" + encodeURIComponent(id),
      { method: "DELETE" });
    if (!r.ok) throw new Error("HTTP " + r.status);
  } catch (e) {
    window.alert("Delete failed: " + e.message);
    return;
  }
  loadPage(false);
}

document.addEventListener("click", function (e) {
  const b = e.target.closest("[data-del]");
  if (b) del(b.getAttribute("data-del"));
});
document.getElementById("studio-search-btn")
  .addEventListener("click", function () { loadPage(false); });
document.getElementById("studio-q")
  .addEventListener("keydown", function (e) {
    if (e.key === "Enter") loadPage(false);
  });
document.getElementById("studio-more")
  .addEventListener("click", function () { loadPage(true); });

loadPage(false);
