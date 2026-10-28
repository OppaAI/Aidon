/* Aidon settings page: renders the /api/settings catalog, saves to .env. */
(function () {
  "use strict";

  const groupsEl = document.getElementById("settings-groups");
  const saveBtn = document.getElementById("settings-save");
  const statusEl = document.getElementById("settings-status");
  // Values as loaded. Save posts ONLY fields the user actually changed:
  // posting every field would freeze catalog defaults into .env for keys
  // that were never set, and a later default change would never apply.
  const initial = {};

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // theme toggle, shared with the chat page
  const rootEl = document.documentElement;
  function setTheme(t) {
    rootEl.dataset.theme = t;
    try { localStorage.setItem("aidon-theme", t); } catch (e) {}
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = t === "dark" ? "◐" : "◑";
  }
  setTheme(localStorage.getItem("aidon-theme") || "dark");
  document.getElementById("theme-toggle").addEventListener("click", () => {
    setTheme(rootEl.dataset.theme === "dark" ? "light" : "dark");
  });

  function fieldFor(s) {
    const id = "set-" + s.key;
    let input;
    if (s.type === "bool") {
      input = '<label class="switch"><input type="checkbox" id="' + id + '"' +
        (String(s.value) === "1" ? " checked" : "") +
        ' data-key="' + esc(s.key) + '"><span></span></label>';
    } else if (s.type === "enum") {
      input = '<select id="' + id + '" data-key="' + esc(s.key) + '">' +
        s.options.map((o) =>
          '<option value="' + esc(o) + '"' +
          (String(s.value) === o ? " selected" : "") + ">" + esc(o) + "</option>"
        ).join("") + "</select>";
    } else {
      const pw = s.type === "password";
      input = '<input id="' + id + '" data-key="' + esc(s.key) + '"' +
        ' type="' + (pw ? "password" : s.type === "int" ? "number" : "text") + '"' +
        ' value="' + esc(pw ? "" : s.value) + '"' +
        (pw ? ' placeholder="' + (s.is_set ? "(set — leave blank to keep)" : "not set") + '"' : "") +
        (pw ? ' autocomplete="new-password"' : "") + ">";
    }
    const badge = s.secret && s.is_set ? ' <span class="set-badge">set</span>' : "";
    return '<div class="set-row">' +
      '<div class="set-label"><label for="' + id + '">' + esc(s.label) + "</label>" +
      '<div class="set-key">' + esc(s.key) + badge + "</div>" +
      (s.desc ? '<div class="set-desc">' + esc(s.desc) + "</div>" : "") + "</div>" +
      '<div class="set-input">' + input + "</div></div>";
  }

  async function load() {
    let r = await fetch("/api/settings/catalog");
    let data = await r.json();
    document.getElementById("env-path").textContent = data.env_file;
    // tab bar: one tab per group, persisted across visits
    const tabsEl = document.createElement("div");
    tabsEl.className = "set-tabs";
    const active = localStorage.getItem("aidon-settings-tab") ||
      data.groups[0].group;
    data.groups.forEach((g) => {
      const t = document.createElement("button");
      t.className = "set-tab" + (g.group === active ? " active" : "");
      t.textContent = g.group;
      t.addEventListener("click", () => {
        localStorage.setItem("aidon-settings-tab", g.group);
        tabsEl.querySelectorAll(".set-tab").forEach((x) =>
          x.classList.remove("active"));
        t.classList.add("active");
        groupsEl.querySelectorAll(".set-group").forEach((s) => {
          s.hidden = s.dataset.group !== g.group;
        });
      });
      tabsEl.appendChild(t);
    });
    groupsEl.before(tabsEl);
    data.groups.forEach((g) => g.settings.forEach((s) => {
      initial[s.key] = s.secret ? "" : String(s.value);
    }));
    groupsEl.innerHTML = data.groups.map((g) =>
      '<section class="set-group" data-group="' + esc(g.group) + '"' +
      (g.group === active ? "" : " hidden") + "><h2>" + esc(g.group) +
      "</h2>" + g.settings.map(fieldFor).join("") + "</section>"
    ).join("");
  }

  async function save() {
    const values = {};
    groupsEl.querySelectorAll("[data-key]").forEach((el) => {
      const k = el.dataset.key;
      const v = el.type === "checkbox" ? (el.checked ? "1" : "0") : el.value;
      if (v !== (initial[k] ?? "")) values[k] = v;
    });
    if (!Object.keys(values).length) {
      statusEl.textContent = "no changes";
      statusEl.className = "";
      return;
    }
    saveBtn.disabled = true;
    statusEl.textContent = "saving…";
    try {
      const r = await fetch("/api/settings", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ settings: values }),
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.detail || r.status);
      statusEl.textContent = "saved " + data.updated.length +
        " setting(s) — restart aidon webui to apply.";
      statusEl.className = "ok";
    } catch (e) {
      statusEl.textContent = "save failed: " + e.message;
      statusEl.className = "err";
    }
    saveBtn.disabled = false;
  }

  saveBtn.addEventListener("click", save);
  load();

  // ── pre-approvals: grant, list, revoke ─────────────────────────────
  // Same store and same rules as WhatsApp, Slack, and the chat box:
  // the reply text there and this panel read <home>/preapprovals.json.
  const preList = document.getElementById("pre-list");
  const preKind = document.getElementById("pre-kind");
  const preStatus = document.getElementById("pre-status");
  const preGrant = document.getElementById("pre-grant");

  function hoursLeft(until) {
    const ms = Math.max(0, until * 1000 - Date.now());
    if (ms >= 3600000) return (ms / 3600000).toFixed(1) + "h left";
    return Math.max(1, Math.round(ms / 60000)) + "m left";
  }

  async function loadPre() {
    if (!preList) return;
    let data;
    try {
      const r = await fetch("/api/preapprovals");
      data = await r.json();
    } catch (e) {
      preStatus.textContent = "could not load pre-approvals";
      return;
    }
    preKind.innerHTML = (data.kinds || []).map((k, i) =>
      '<option value="' + esc(k.name) + '">' + (i + 1) + ". " + esc(k.label) +
      "</option>").join("");
    const grants = data.grants || [];
    preList.innerHTML = grants.length ? grants.map((g) =>
      '<li><span>' + esc(g.spec_id) + " · <strong>" + esc(g.action_kind) +
      "</strong> · " + hoursLeft(Number(g.until)) + "</span>" +
      '<button type="button" data-revoke="' + esc(g.spec_id) +
      '" data-kind="' + esc(g.action_kind) + '">Revoke</button></li>').join("")
      : '<li class="pre-empty">Nothing pre-approved.</li>';
  }

  if (preList) {
    preList.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("button[data-revoke]");
      if (!btn) return;
      const spec = btn.dataset.revoke;
      const kind = btn.dataset.kind;
      const r = await fetch("/api/preapprovals/" + encodeURIComponent(spec) +
        "/" + encodeURIComponent(kind), { method: "DELETE" });
      const d = await r.json();
      preStatus.textContent = d.ok ? "revoked" : (d.error || "not revoked");
      loadPre();
    });
  }

  if (preGrant) {
    preGrant.addEventListener("click", async () => {
      const spec = (document.getElementById("pre-project").value || "default").trim();
      const kind = preKind.value;
      const hours = Number(document.getElementById("pre-hours").value || 8);
      preStatus.textContent = "saving…";
      const r = await fetch("/api/preapprovals", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec_id: spec, kind: kind, hours: hours }),
      });
      const d = await r.json();
      preStatus.textContent = d.ok ? "pre-approved" : (d.error || "refused");
      loadPre();
    });
  }

  loadPre();

  // ── API keys: name, scope, hash prefix only ────────────────────────
  async function loadKeys() {
    const list = document.getElementById("key-list");
    if (!list) return;
    let keys = [];
    try {
      const r = await fetch("/api/keys");
      keys = (await r.json()).keys || [];
    } catch (e) {
      list.innerHTML = '<li class="pre-empty">could not load keys</li>';
      return;
    }
    list.innerHTML = keys.length ? keys.map((k) =>
      "<li><span>" + esc(k.name) + " · " + esc(k.scope) + "</span><code>" +
      esc(k.key_hash_prefix) + "</code></li>").join("")
      : '<li class="pre-empty">No API keys.</li>';
  }

  loadKeys();
})();
