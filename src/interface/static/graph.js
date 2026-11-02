"use strict";
// Memory Graph page: render /api/memory/graph as nodes and edges.
// Point nodes link to the entity nodes they share; entities are the
// connectors, so the picture is bipartite by construction.

try {
  document.documentElement.setAttribute(
    "data-theme", localStorage.getItem("aidon-theme") || "light");
} catch (e) { /* private mode: keep the default theme */ }

const COLLECTION_COLORS = {
  working: "#8b949e",
  episodic: "#6cb6ff",
  semantic: "#4caf7d",
  experience: "#d29922",
  knowledge: "#b392f0",
};
const ENTITY_COLOR = "#e3b341";
const EDGE_COLOR = "rgba(140, 160, 180, 0.35)";
const DIMMED = "rgba(140, 160, 180, 0.10)";

const canvas = document.getElementById("graph-canvas");
const ctx = canvas.getContext("2d");
const countEl = document.getElementById("graph-count");
const detailEl = document.getElementById("graph-detail");
const errorEl = document.getElementById("graph-error");
const collectionEl = document.getElementById("graph-collection");

let nodes = [];
let edges = [];
let neighbors = new Map();
let selected = null;
let hovered = null;
let dragging = null;
let running = false;
let frame = 0;

// Deterministic start positions, so reloads settle the same way.
function seedRand(seed) {
  let t = seed >>> 0;
  return function () {
    t = (t + 0x6d2b79f5) >>> 0;
    let r = t;
    r = Math.imul(r ^ (r >>> 15), r | 1);
    r ^= r + Math.imul(r ^ (r >>> 7), r | 61);
    return ((r ^ (r >>> 14)) >>> 0) / 4294967296;
  };
}

function hashStr(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h;
}

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
}

function load() {
  const collection = collectionEl.value;
  const url = "/api/memory/graph?limit=500" +
    (collection ? "&collection=" + encodeURIComponent(collection) : "");
  errorEl.style.display = "none";
  fetch(url).then((r) => r.json()).then((data) => {
    if (data.error) throw new Error(data.error);
    build(data.nodes || [], data.edges || []);
  }).catch((err) => {
    errorEl.textContent = "Could not load the graph: " + err.message;
    errorEl.style.display = "block";
  });
}

function build(rawNodes, rawEdges) {
  const w = canvas.clientWidth || 800;
  const h = canvas.clientHeight || 620;
  nodes = rawNodes.map((n) => {
    const rand = seedRand(hashStr(n.id));
    return {
      id: n.id,
      type: n.type,
      label: n.label || n.id,
      collection: n.collection || "",
      x: 60 + rand() * (w - 120),
      y: 60 + rand() * (h - 120),
      vx: 0,
      vy: 0,
      r: n.type === "entity" ? 8 : 5,
    };
  });
  const byId = new Map(nodes.map((n) => [n.id, n]));
  edges = rawEdges.filter((e) => byId.has(e.source) && byId.has(e.target));
  neighbors = new Map();
  for (const n of nodes) neighbors.set(n.id, new Set());
  for (const e of edges) {
    neighbors.get(e.source).add(e.target);
    neighbors.get(e.target).add(e.source);
  }
  // Entities connect more points, so they get a slightly larger radius.
  for (const n of nodes) {
    n.r += Math.min(4, (neighbors.get(n.id).size - 1) * 0.5);
  }
  selected = null;
  detailEl.textContent = "";
  countEl.textContent = nodes.length + " nodes · " + edges.length + " edges";
  if (!nodes.length) {
    countEl.textContent = "No memory or knowledge points yet.";
    draw();
    return;
  }
  frame = 0;
  if (!running) {
    running = true;
    requestAnimationFrame(tick);
  }
}

function tick() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  const cx = w / 2;
  const cy = h / 2;
  // Repulsion between all pairs, springs along edges, pull to center.
  for (let i = 0; i < nodes.length; i++) {
    const a = nodes[i];
    if (a === dragging) continue;
    for (let j = i + 1; j < nodes.length; j++) {
      const b = nodes[j];
      let dx = a.x - b.x;
      let dy = a.y - b.y;
      let d2 = dx * dx + dy * dy;
      if (d2 < 1) { dx = 1; dy = 0; d2 = 1; }
      const d = Math.sqrt(d2);
      const f = Math.min(900 / d2, 2);
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      if (b !== dragging) { b.vx -= fx; b.vy -= fy; }
      a.vx += fx; a.vy += fy;
    }
  }
  for (const e of edges) {
    const a = nodes.find((n) => n.id === e.source);
    const b = nodes.find((n) => n.id === e.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const d = Math.sqrt(dx * dx + dy * dy) || 1;
    const f = (d - 90) * 0.02;
    const fx = (dx / d) * f;
    const fy = (dy / d) * f;
    if (a !== dragging) { a.vx += fx; a.vy += fy; }
    if (b !== dragging) { b.vx -= fx; b.vy -= fy; }
  }
  let maxV = 0;
  for (const n of nodes) {
    if (n === dragging) continue;
    n.vx += (cx - n.x) * 0.002;
    n.vy += (cy - n.y) * 0.002;
    n.vx *= 0.85;
    n.vy *= 0.85;
    n.x += n.vx;
    n.y += n.vy;
    n.x = Math.max(20, Math.min(w - 20, n.x));
    n.y = Math.max(20, Math.min(h - 20, n.y));
    maxV = Math.max(maxV, Math.abs(n.vx) + Math.abs(n.vy));
  }
  draw();
  frame++;
  if (maxV > 0.05 && frame < 900) {
    requestAnimationFrame(tick);
  } else {
    running = false;
  }
}

function nodeAt(x, y) {
  let best = null;
  let bestD = 18;
  for (const n of nodes) {
    const d = Math.hypot(n.x - x, n.y - y);
    if (d < bestD + n.r) { best = n; bestD = d; }
  }
  return best;
}

function draw() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  ctx.clearRect(0, 0, w, h);
  const keep = selected
    ? new Set([selected.id, ...neighbors.get(selected.id)])
    : null;
  ctx.lineWidth = 1;
  for (const e of edges) {
    const a = nodes.find((n) => n.id === e.source);
    const b = nodes.find((n) => n.id === e.target);
    if (!a || !b) continue;
    ctx.strokeStyle = keep && (!keep.has(a.id) || !keep.has(b.id))
      ? DIMMED : EDGE_COLOR;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  }
  for (const n of nodes) {
    const dimmed = keep && !keep.has(n.id);
    ctx.globalAlpha = dimmed ? 0.25 : 1;
    ctx.fillStyle = n.type === "entity"
      ? ENTITY_COLOR : (COLLECTION_COLORS[n.collection] || "#8b949e");
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
    ctx.fill();
    if (n.type === "entity" || n === selected || n === hovered) {
      ctx.font = "11px sans-serif";
      ctx.fillStyle = dimmed ? "#666" : "#ccc";
      ctx.fillText(n.label, n.x + n.r + 4, n.y + 4);
    }
    ctx.globalAlpha = 1;
  }
}

function pos(evt) {
  const rect = canvas.getBoundingClientRect();
  return { x: evt.clientX - rect.left, y: evt.clientY - rect.top };
}

canvas.addEventListener("pointerdown", (evt) => {
  const p = pos(evt);
  const n = nodeAt(p.x, p.y);
  if (!n) return;
  dragging = n;
  canvas.setPointerCapture(evt.pointerId);
  if (!running) {
    running = true;
    frame = 0;
    requestAnimationFrame(tick);
  }
});

canvas.addEventListener("pointermove", (evt) => {
  const p = pos(evt);
  if (dragging) {
    dragging.x = p.x;
    dragging.y = p.y;
    dragging.vx = 0;
    dragging.vy = 0;
    draw();
    return;
  }
  const n = nodeAt(p.x, p.y);
  if (n !== hovered) {
    hovered = n;
    draw();
  }
});

canvas.addEventListener("pointerup", (evt) => {
  if (!dragging) return;
  const wasDragged = dragging;
  dragging = null;
  // A click (no real move) selects; a drag just repositions.
  const p = pos(evt);
  if (Math.hypot(wasDragged.x - p.x, wasDragged.y - p.y) < 4) {
    selected = wasDragged === selected ? null : wasDragged;
    if (selected) {
      const links = [...neighbors.get(selected.id)].length;
      detailEl.textContent =
        (selected.type === "entity" ? "Entity: " : "Point (" +
          (selected.collection || "memory") + "): ") +
        selected.label + "\n" + links + " connection(s).";
    } else {
      detailEl.textContent = "";
    }
    draw();
  }
});

document.getElementById("graph-refresh").addEventListener("click", load);
collectionEl.addEventListener("change", load);
window.addEventListener("resize", () => { resize(); draw(); });

resize();
load();
