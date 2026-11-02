/* Aidon DAG editor - n8n-style view/edit for the graphs Aidon builds.
 *
 * Plain SVG + vanilla JS: the graph is small (a handful of nodes), so
 * a framework would cost more than it saves. The server owns validation
 * and layout; this file owns interaction.
 */
"use strict";

const NS = "http://www.w3.org/2000/svg";
const NODE_W = 270;
const NODE_H = 88;

const S = {
  dags: [],
  cur: null,          // name of the open graph
  dag: null,          // {name, goal, nodes:[...]}
  layout: {},         // node id -> {x, y}
  sel: null,          // selected node id
  tools: [],
  dirty: false,
  view: { x: 0, y: 0, k: 1 },
};

const $ = (s) => document.querySelector(s);
const svg = $("#canvas");
const gNodes = $("#nodes");
const gEdges = $("#edges");
const gView = $("#viewport");
const gridBg = $("#grid-bg");
const linking = $("#linking");

// ---------- plumbing -------------------------------------------------------

async function api(path, opts) {
  const r = await fetch(path, opts);
  let data = null;
  try { data = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) {
    const msg = (data && (data.detail || data.error)) || r.statusText;
    throw new Error(msg);
  }
  return data;
}

function normalizeToolList(data) {
  if (Array.isArray(data)) return data;
  return data && Array.isArray(data.tools) ? data.tools : [];
}

function status(msg, kind) {
  const el = $("#status");
  el.textContent = msg;
  el.className = "dag-status" + (kind ? " " + kind : "");
}

function markDirty() {
  S.dirty = true;
  status("unsaved changes", "warn");
}

// ---------- the list --------------------------------------------------------

async function loadList() {
  try {
    const data = await api("/api/dags");
    S.dags = (Array.isArray(data) ? data : (data.dags || [])).map((d) => ({
      ...d, name: d.name || d.id, node_count: d.node_count || d.nodes || 0,
    }));
    renderList();
  } catch (e) {
    status("could not load graphs: " + e.message, "err");
  }
}

function renderList() {
  const q = $("#search").value.trim().toLowerCase();
  const box = $("#dag-list");
  box.innerHTML = "";
  const items = S.dags.filter((d) => !q
    || d.name.toLowerCase().includes(q)
    || (d.goal || "").toLowerCase().includes(q));
  if (!items.length) {
    box.innerHTML = '<div class="dag-none">no graphs match</div>';
    return;
  }
  items.forEach((d) => {
    const el = document.createElement("div");
    el.className = "dag-item" + (S.cur === d.name ? " active" : "");
    const name = document.createElement("div");
    name.className = "dag-item-name";
    name.textContent = d.name;
    const goal = document.createElement("div");
    goal.className = "dag-item-goal";
    goal.textContent = d.goal || "";
    const meta = document.createElement("div");
    meta.className = "dag-item-meta";
    meta.textContent = (d.node_count || 0) + " nodes · "
      + (d.created_at || "").slice(0, 16).replace("T", " ");
    el.append(name, goal, meta);
    el.addEventListener("click", () => openDag(d.name));
    box.appendChild(el);
  });
}

function isLegacyLearnedSequence(nodes) {
  return nodes.length > 1 && nodes.every((node, index) => {
    const expected = index + 1;
    return (!String(node.name || "").trim()
      && (node.id === `step-${expected}` || node.id === `step_${expected}`)
      && !(node.deps || []).length);
  });
}

function layoutMap(data) {
  const raw = data && data.layout || {};
  return Array.isArray(raw)
    ? Object.fromEntries(raw.map((point) => [point.id, point])) : raw;
}

async function requestLayout(dag) {
  const res = await api("/api/dags/layout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dag: { ...dag, nodes: dag.nodes.map(
      ({ deps, ...node }) => ({ ...node, depends_on: deps || [] })) } }),
  });
  return layoutMap(res);
}

// ---------- open / close ----------------------------------------------------

async function openDag(name) {
  if (S.dirty && !confirm("Discard unsaved graph changes and open another workflow?")) return;
  try {
    const data = await api("/api/dags/" + encodeURIComponent(name));
    S.cur = name;
    S.dag = data.dag || data;
    S.dag.nodes = (S.dag.nodes || []).map((node) => ({
      ...node, deps: node.deps || node.depends_on || [],
      args: node.args || {},
    }));
    const inferredSequence = isLegacyLearnedSequence(S.dag.nodes);
    if (inferredSequence) {
      S.dag.nodes.forEach((node, index) => {
        if (index) node.deps = [S.dag.nodes[index - 1].id];
      });
    }
    $("#dag-goal").value = S.dag.goal || "";
    S.layout = layoutMap(data);
    const needsLayout = S.dag.nodes.some((node) => {
      const point = S.layout[node.id];
      return !point || !Number.isFinite(Number(point.x))
        || !Number.isFinite(Number(point.y));
    });
    if (needsLayout && S.dag.nodes.length) {
      try {
        S.layout = await requestLayout(S.dag);
      } catch (e) {
        // Keep the graph inspectable if the layout endpoint is unavailable;
        // the auto-layout button can retry once the service is reachable.
      }
    }
    S.sel = null;
    // Dependency inference and auto-placement are view-time repairs for
    // legacy learned DAGs. They can be saved explicitly, but opening a graph
    // alone must not trigger a destructive-change confirmation.
    S.dirty = false;
    S.view = { x: 0, y: 0, k: 1 };
    renderList();
    renderGraph();
    renderInspector();
    if (inferredSequence || needsLayout) {
      fitGraph();
      status(inferredSequence
        ? "legacy run order shown as dependencies · save to keep"
        : "missing node positions arranged · save to keep", "warn");
    } else {
      status("opened " + name);
    }
  } catch (e) {
    status("could not open: " + e.message, "err");
  }
}

function closeDag() {
  if (S.dirty && !confirm("Discard unsaved graph changes?")) return;
  S.cur = null;
  S.dag = null;
  S.layout = {};
  S.sel = null;
  renderList();
  renderGraph();
  renderInspector();
}

// ---------- graph rendering -------------------------------------------------

function nodeById(id) {
  return (S.dag && S.dag.nodes || []).find((n) => n.id === id) || null;
}

function renderGraph() {
  gNodes.innerHTML = "";
  gEdges.innerHTML = "";
  if (!S.dag) return;
  const nodes = S.dag.nodes || [];

  nodes.forEach((n) => {
    (n.deps || []).forEach((dep) => {
      const a = S.layout[dep];
      const b = S.layout[n.id];
      if (!a || !b) return;
      const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
      const x2 = b.x, y2 = b.y + NODE_H / 2;
      const dx = Math.max(45, Math.abs(x2 - x1) / 2);
      const p = document.createElementNS(NS, "path");
      p.setAttribute("d", `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`);
      p.setAttribute("class", "dag-edge");
      p.setAttribute("marker-end", "url(#arrow)");
      gEdges.appendChild(p);
    });
  });

  nodes.forEach((n) => {
    const pos = S.layout[n.id] || { x: 60, y: 50 };
    S.layout[n.id] = pos;
    const g = document.createElementNS(NS, "g");
    g.setAttribute("class", "dag-node" + (S.sel === n.id ? " selected" : ""));
    g.setAttribute("transform", `translate(${pos.x},${pos.y})`);
    g.dataset.id = n.id;

    const rect = document.createElementNS(NS, "rect");
    rect.setAttribute("width", NODE_W);
    rect.setAttribute("height", NODE_H);
    rect.setAttribute("rx", 12);
    rect.setAttribute("class", "dag-node-box");
    g.appendChild(rect);

    const title = document.createElementNS(NS, "text");
    title.setAttribute("x", 16);
    title.setAttribute("y", 28);
    title.setAttribute("class", "dag-node-title");
    title.textContent = n.name || n.id;
    g.appendChild(title);

    const badge = document.createElementNS(NS, "text");
    badge.setAttribute("x", 16);
    badge.setAttribute("y", 50);
    badge.setAttribute("class", "dag-node-tool");
    badge.textContent = n.tool + (n.executor && n.executor !== "tool"
      ? " · " + n.executor : "");
    g.appendChild(badge);

    const nid = document.createElementNS(NS, "text");
    nid.setAttribute("x", NODE_W - 16);
    nid.setAttribute("y", 50);
    nid.setAttribute("class", "dag-node-id");
    nid.setAttribute("text-anchor", "end");
    nid.textContent = n.id;
    g.appendChild(nid);

    const portIn = document.createElementNS(NS, "circle");
    portIn.setAttribute("cx", 0);
    portIn.setAttribute("cy", NODE_H / 2);
    portIn.setAttribute("r", 6);
    portIn.setAttribute("class", "dag-port dag-port-in");
    g.appendChild(portIn);

    const portOut = document.createElementNS(NS, "circle");
    portOut.setAttribute("cx", NODE_W);
    portOut.setAttribute("cy", NODE_H / 2);
    portOut.setAttribute("r", 7);
    portOut.setAttribute("class", "dag-port dag-port-out");
    portOut.dataset.id = n.id;
    g.appendChild(portOut);

    g.addEventListener("mousedown", (ev) => onNodeDown(ev, n));
    gNodes.appendChild(g);
  });
}

// ---------- interaction: drag, connect, pan, zoom ---------------------------

let drag = null;   // {kind, ...}

function svgPoint(ev) {
  const rect = svg.getBoundingClientRect();
  return {
    x: (ev.clientX - rect.left - S.view.x) / S.view.k,
    y: (ev.clientY - rect.top - S.view.y) / S.view.k,
  };
}

function applyView() {
  gView.setAttribute("transform",
    `translate(${S.view.x},${S.view.y}) scale(${S.view.k})`);
}

function onNodeDown(ev, node) {
  ev.stopPropagation();
  if (ev.target.classList.contains("dag-port-out")) {
    startLink(ev, node);
    return;
  }
  S.sel = node.id;
  renderGraph();
  renderInspector();
  const p = svgPoint(ev);
  drag = { kind: "node", id: node.id, dx: p.x - S.layout[node.id].x,
           dy: p.y - S.layout[node.id].y };
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
}

function startLink(ev, node) {
  ev.stopPropagation();
  const p = svgPoint(ev);
  drag = { kind: "link", from: node.id, x: p.x, y: p.y };
  linking.style.display = "";
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
}

function onMove(ev) {
  if (!drag) return;
  const p = svgPoint(ev);
  if (drag.kind === "node") {
    S.layout[drag.id] = { x: p.x - drag.dx, y: p.y - drag.dy };
    renderGraph();
  } else if (drag.kind === "link") {
    drag.x = p.x;
    drag.y = p.y;
    const a = S.layout[drag.from] || { x: 0, y: 0 };
    const x1 = a.x + NODE_W, y1 = a.y + NODE_H / 2;
    const dx = Math.max(45, Math.abs(p.x - x1) / 2);
    linking.setAttribute("d",
      `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${p.x - dx} ${p.y}, ${p.x} ${p.y}`);
  } else if (drag.kind === "pan") {
    // Delta from the grab point PLUS the view offset at grab time:
    // using the absolute cursor made the canvas jump on the first move
    // whenever the view was not already at (0,0).
    S.view.x = drag.vx + (ev.clientX - drag.px);
    S.view.y = drag.vy + (ev.clientY - drag.py);
    applyView();
  }
}

function onUp(ev) {
  if (drag && drag.kind === "link") {
    linking.style.display = "none";
    const target = nodeUnder(ev);
    if (target && target !== drag.from) toggleDep(drag.from, target);
  }
  if (drag && drag.kind === "node") markDirty();
  drag = null;
  window.removeEventListener("mousemove", onMove);
  window.removeEventListener("mouseup", onUp);
}

function nodeUnder(ev) {
  const el = document.elementFromPoint(ev.clientX, ev.clientY);
  const g = el && el.closest ? el.closest(".dag-node") : null;
  return g ? g.dataset.id : null;
}

function toggleDep(from, to) {
  const target = nodeById(to);
  const source = nodeById(from);
  if (!target || !source) return;
  target.deps = target.deps || [];
  const i = target.deps.indexOf(from);
  if (i >= 0) {
    target.deps.splice(i, 1);
  } else {
    target.deps.push(from);
  }
  markDirty();
  renderGraph();
  renderInspector();
}

svg.addEventListener("mousedown", (ev) => {
  if (ev.target.closest && ev.target.closest(".dag-node")) return;
  S.sel = null;
  renderGraph();
  renderInspector();
  drag = { kind: "pan", px: ev.clientX, py: ev.clientY,
           vx: S.view.x, vy: S.view.y };
  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);
});

svg.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const rect = svg.getBoundingClientRect();
  const mx = ev.clientX - rect.left;
  const my = ev.clientY - rect.top;
  const k0 = S.view.k;
  const k = Math.min(2.2, Math.max(0.25, k0 * (ev.deltaY < 0 ? 1.1 : 0.9)));
  S.view.x = mx - (mx - S.view.x) * (k / k0);
  S.view.y = my - (my - S.view.y) * (k / k0);
  S.view.k = k;
  applyView();
}, { passive: false });

// ---------- inspector -------------------------------------------------------

function renderInspector() {
  const empty = $("#inspector-empty");
  const panel = $("#inspector");
  const node = S.sel ? nodeById(S.sel) : null;
  if (!node) {
    empty.hidden = false;
    panel.hidden = true;
    return;
  }
  empty.hidden = true;
  panel.hidden = false;
  $("#insp-id").textContent = node.id;
  $("#f-name").value = node.name || "";
  $("#f-deliverable").value = node.deliverable || "";
  $("#f-tool").value = node.tool || "";
  $("#f-args").value = JSON.stringify(node.args || {}, null, 2);
  $("#f-inputs").value = (node.inputs || []).join("\n");
  $("#f-outputs").value = (node.outputs || []).join("\n");
  $("#f-preconditions").value = (node.preconditions || []).join("\n");
  $("#f-expected").value = node.expected || "";
  $("#f-assumptions").value = (node.assumptions || []).join("\n");
  $("#f-executor").value = node.executor || "tool";
  $("#insp-state").textContent = "";

  const deps = $("#f-deps");
  deps.innerHTML = "";
  (S.dag.nodes || []).filter((n) => n.id !== node.id).forEach((other) => {
    const label = document.createElement("label");
    label.className = "dag-dep";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = (node.deps || []).includes(other.id);
    cb.addEventListener("change", () => {
      node.deps = node.deps || [];
      const i = node.deps.indexOf(other.id);
      if (cb.checked && i < 0) node.deps.push(other.id);
      if (!cb.checked && i >= 0) node.deps.splice(i, 1);
      markDirty();
      renderGraph();
    });
    const span = document.createElement("span");
    span.textContent = other.name || other.id;
    label.append(cb, span);
    deps.appendChild(label);
  });
}

function bindInspector() {
  $("#f-name").addEventListener("input", (e) => {
    const n = nodeById(S.sel);
    if (!n) return;
    n.name = e.target.value;
    markDirty();
    renderGraph();
  });

  $("#f-deliverable").addEventListener("input", (e) => {
    const n = nodeById(S.sel); if (!n) return;
    n.deliverable = e.target.value; markDirty(); renderGraph();
  });
  [["#f-inputs", "inputs"], ["#f-outputs", "outputs"],
   ["#f-preconditions", "preconditions"], ["#f-assumptions", "assumptions"]]
    .forEach(([selector, key]) => $(selector).addEventListener("change", (e) => {
      const n = nodeById(S.sel); if (!n) return;
      n[key] = e.target.value.split("\n").map(x => x.trim()).filter(Boolean);
      markDirty();
    }));
  $("#f-expected").addEventListener("change", (e) => {
    const n = nodeById(S.sel); if (!n) return;
    n.expected = e.target.value.trim(); markDirty();
  });

  $("#f-tool").addEventListener("change", (e) => {
    const n = nodeById(S.sel);
    if (!n) return;
    n.tool = e.target.value;
    markDirty();
    renderGraph();
  });

  $("#f-executor").addEventListener("change", (e) => {
    const n = nodeById(S.sel);
    if (!n) return;
    n.executor = e.target.value;
    markDirty();
    renderGraph();
  });

  $("#f-args").addEventListener("change", (e) => {
    const n = nodeById(S.sel);
    if (!n) return;
    const raw = (e.target.value || "").trim();
    if (!raw) {
      n.args = {};
      markDirty();
      return;
    }
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("args must be a JSON object");
      }
      n.args = parsed;
      $("#insp-state").textContent = "";
      markDirty();
    } catch (err) {
      $("#insp-state").textContent = "args: " + err.message;
      $("#insp-state").className = "dag-note dag-note-err";
    }
  });

  $("#btn-delnode").addEventListener("click", () => {
    const n = nodeById(S.sel);
    if (!n) return;
    if (!confirm(`Delete node "${n.name || n.id}"?`)) return;
    S.dag.nodes = S.dag.nodes.filter((x) => x.id !== n.id);
    S.dag.nodes.forEach((x) => {
      x.deps = (x.deps || []).filter((d) => d !== n.id);
    });
    delete S.layout[n.id];
    S.sel = null;
    markDirty();
    renderGraph();
    renderInspector();
  });

  $("#btn-dup").addEventListener("click", () => {
    const n = nodeById(S.sel);
    if (!n) return;
    const id = uniqueId(n.id + "-copy");
    const copy = JSON.parse(JSON.stringify(n));
    copy.id = id;
    copy.name = (n.name || n.id) + " (copy)";
    copy.deps = (n.deps || []).slice();
    S.dag.nodes.push(copy);
    S.layout[id] = {
      x: (S.layout[n.id]?.x || 60) + 60,
      y: (S.layout[n.id]?.y || 50) + 40,
    };
    S.sel = id;
    markDirty();
    renderGraph();
    renderInspector();
  });
}

function uniqueId(base) {
  const ids = new Set((S.dag?.nodes || []).map((n) => n.id));
  let id = base;
  let i = 2;
  while (ids.has(id)) id = base + "-" + i++;
  return id;
}

// ---------- actions ---------------------------------------------------------

async function saveDag() {
  if (!S.cur || !S.dag) {
    status("nothing to save", "warn");
    return;
  }
  const failures = validateGraph();
  if (failures.length) {
    status("cannot save: " + failures[0], "err");
    return;
  }
  try {
    const res = await api("/api/dags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: S.cur, goal: S.dag.goal || "",
        nodes: (S.dag.nodes || []).map(({ deps, ...n }) => ({
          ...n, depends_on: deps || [],
        })), layout: S.layout }),
    });
    S.dirty = false;
    status("saved " + S.cur, "ok");
    if (res && res.layout) {
      // keep the human's arrangement; only fill in nodes that had none
      Object.entries(res.layout).forEach(([id, pos]) => {
        if (!S.layout[id]) S.layout[id] = pos;
      });
    }
    renderGraph();
    loadList();
  } catch (e) {
    status("not saved: " + e.message, "err");
  }
}

async function newDag() {
  if (S.dirty && !confirm("Discard unsaved graph changes and create a workflow?")) return;
  const name = prompt("Name for the new graph:");
  if (!name) return;
  try {
    await api("/api/dags", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: name.trim(), goal: "", nodes: [],
      }),
    });
    await loadList();
    await openDag(name.trim());
    status("created " + name.trim(), "ok");
  } catch (e) {
    status("could not create: " + e.message, "err");
  }
}

async function deleteDag() {
  if (!S.cur) {
    status("no graph selected", "warn");
    return;
  }
  if (!confirm(`Delete the graph "${S.cur}"? This cannot be undone.`)) return;
  try {
    await api("/api/dags/" + encodeURIComponent(S.cur), { method: "DELETE" });
    status("deleted " + S.cur, "ok");
    closeDag();
    await loadList();
  } catch (e) {
    status("could not delete: " + e.message, "err");
  }
}

function addNode() {
  if (!S.dag) {
    status("open a graph first", "warn");
    return;
  }
  const id = uniqueId("step" + ((S.dag.nodes || []).length + 1));
  const node = {
    id: id,
    name: "New step",
    tool: (S.tools[0] && S.tools[0].name) || "shell",
    args: {},
    deps: [],
    inputs: [], outputs: [], preconditions: [], assumptions: [],
    expected: "", deliverable: "",
  };
  S.dag.nodes.push(node);
  const maxX = Object.values(S.layout).reduce(
    (m, p) => Math.max(m, p.x), 60);
  S.layout[id] = { x: maxX + 60, y: 50 };
  S.sel = id;
  markDirty();
  renderGraph();
  renderInspector();
}

async function autoLayout() {
  if (!S.dag) return;
  try {
    // The server owns the layout rule - one implementation, not two.
    S.layout = await requestLayout(S.dag);
    renderGraph();
    markDirty();
    fitGraph();
    status("relaid out", "ok");
  } catch (e) {
    status("layout failed: " + e.message, "err");
  }
}

function exportJson() {
  if (!S.dag) return;
  const blob = new Blob([JSON.stringify(S.dag, null, 2)],
    { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = (S.cur || "graph") + ".json";
  a.click();
  URL.revokeObjectURL(a.href);
}

function validateGraph() {
  const nodes = S.dag?.nodes || [];
  const errors = [];
  const ids = nodes.map(n => String(n.id || "").trim());
  if (!nodes.length) errors.push("workflow needs at least one node");
  if (ids.some(id => !id)) errors.push("every node needs an id");
  if (new Set(ids).size !== ids.length) errors.push("node ids must be unique");
  const known = new Set(ids);
  for (const node of nodes) {
    for (const dep of node.deps || []) {
      if (!known.has(dep)) errors.push(`${node.id} depends on missing node ${dep}`);
      if (dep === node.id) errors.push(`${node.id} cannot depend on itself`);
    }
  }
  const visiting = new Set(), visited = new Set();
  function visit(id) {
    if (visiting.has(id)) return false;
    if (visited.has(id)) return true;
    visiting.add(id);
    const node = nodes.find(n => n.id === id);
    for (const dep of (node?.deps || [])) if (known.has(dep) && !visit(dep)) return false;
    visiting.delete(id); visited.add(id); return true;
  }
  if (ids.some(id => known.has(id) && !visit(id))) errors.push("workflow contains a dependency cycle");
  const outputs = nodes.map(n => String(n.deliverable || "").trim()).filter(Boolean);
  if (new Set(outputs).size !== outputs.length) errors.push("deliverable paths must be unique");
  return [...new Set(errors)];
}

function fitGraph() {
  const nodes = S.dag?.nodes || [];
  if (!nodes.length) return;
  const points = nodes.map(n => S.layout[n.id]).filter(Boolean);
  if (!points.length) return;
  const rect = svg.getBoundingClientRect();
  const minX = Math.min(...points.map(p => p.x));
  const minY = Math.min(...points.map(p => p.y));
  const maxX = Math.max(...points.map(p => p.x + NODE_W));
  const maxY = Math.max(...points.map(p => p.y + NODE_H));
  const scale = Math.min(1.8, Math.max(0.25,
    Math.min((rect.width - 80) / (maxX - minX + 40),
             (rect.height - 80) / (maxY - minY + 40))));
  S.view.k = scale;
  S.view.x = (rect.width - (maxX - minX) * scale) / 2 - minX * scale;
  S.view.y = (rect.height - (maxY - minY) * scale) / 2 - minY * scale;
  applyView();
}

// ---------- boot ------------------------------------------------------------

async function boot() {
  bindInspector();
  $("#btn-save").addEventListener("click", saveDag);
  $("#btn-new").addEventListener("click", newDag);
  $("#btn-add").addEventListener("click", addNode);
  $("#btn-layout").addEventListener("click", autoLayout);
  $("#btn-fit").addEventListener("click", fitGraph);
  $("#btn-validate").addEventListener("click", () => {
    const errors = validateGraph();
    status(errors.length ? errors.join(" · ") : "workflow is structurally valid", errors.length ? "err" : "ok");
  });
  $("#btn-export").addEventListener("click", exportJson);
  $("#btn-delete").addEventListener("click", deleteDag);
  $("#search").addEventListener("input", renderList);
  $("#dag-goal").addEventListener("input", (e) => {
    if (!S.dag) return;
    S.dag.goal = e.target.value; markDirty();
  });

  // Load saved workflows independently so an unavailable/malformed tool
  // catalog cannot leave the DAG list blank during editor startup.
  const dagListReady = loadList();
  try {
    S.tools = normalizeToolList(await api("/api/tools"));
  } catch (e) {
    S.tools = [];
  }
  const sel = $("#f-tool");
  sel.innerHTML = "";
  S.tools.forEach((t) => {
    const o = document.createElement("option");
    o.value = t.name;
    o.textContent = t.name;
    o.title = t.description || "";
    sel.appendChild(o);
  });
  // "llm" is a valid node tool even though it is not a registered tool
  if (!S.tools.some((t) => t.name === "llm")) {
    const o = document.createElement("option");
    o.value = "llm";
    o.textContent = "llm (model call)";
    sel.appendChild(o);
  }

  applyView();
  await dagListReady;
}

boot();
