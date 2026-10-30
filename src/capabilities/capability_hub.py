"""Capability Hub: the local capability index.

Aidon already has stores for what it can do. It has no
single read path for the question every planning step
asks: what can I do, what is trusted, and what is
currently healthy. Triage assembles its own inventory
from tool names, skill texts, and MCP names; the live
registry view lists sandbox and plugin tools plus skill
lifecycle states but triage never reads it; discovery
search covers sandbox and plugin tools only; MCP tools
are invisible until a server is connected for one use;
images, registry entries, and failure lessons are
invisible to every planning path.

The hub closes that gap as an index, not a warehouse.
It owns no capability and no store of record. Each
index() call rebuilds entries fresh from the live
stores, so the index cannot drift from what is
actually installed, and writers keep writing where
they always wrote: adoption to plugins/, skills to
skills.json, images to capability_images.json, MCP
registrations to capabilities.json, operator servers
to mcp_servers.json. The one thing the hub does own
is the health cache (see below).

An entry carries independent dimensions, not a single
status, because a single status lies: a trusted MCP
server can be offline, and a connected server can
expose untrusted tools.

  id            "kind:name", unique across the index
  name, kind    kind: tool | plugin | skill | mcp | image
  lifecycle     builtin | candidate | validated | trusted
                | retired. "builtin" is the arena's own
                tools: not lifecycle-gated, always usable,
                and deliberately distinct from "trusted",
                which means evidence plus human approval.
  health        healthy | degraded | unavailable | unknown
  health_checked_at   timestamp of the last check, or None
  availability  local | remote | disconnected.
                local runs on this machine (plugins,
                skills, stdio MCP servers); remote runs
                elsewhere (http MCP servers, Modal
                images); disconnected means the hub knows
                the capability but its runnable path is
                absent (a registry image with no manifest,
                an MCP config with no command or url).
  verification  untested | passed | failed. "passed"
                means test evidence exists and passed:
                registry registration requires it, the
                builder's locked-sandbox test provides
                it, adoption tested and scanned the
                plugin. Anything without evidence is
                untested, never assumed passing.
  permissions   declared permissions only, never
                inferred. Undeclared means an empty list,
                not "safe": http MCP servers declare
                network; everything else declares nothing
                in v1, and MCP acquisition is where richer
                declarations will come from.
  provenance    built | installed | operator. Built
                means Aidon created it (a distilled skill,
                a built image, a registered capability);
                installed means it shipped with Aidon
                (core tools); operator means a human
                placed or configured it (adopted plugins,
                hand-placed skill files, mcp_servers.json).
  source        pointer to the store of record, e.g.
                "plugins/<name>", "skills.json:<name>",
                "capabilities.json:<name>".
  tools         tool names the capability exposes
                (plugins list theirs; MCP entries list
                their claimed tools; skills and images
                expose none directly).
  description   short human-readable summary.

Precedence, when two sources describe the same
capability: the richest record wins. The registry
beats operator config and image manifests; the skill
library beats plugin-bundled and hand-placed skill
files; a hand-placed skills/*.md file whose stem
matches a library skill is treated as that skill's
synced discovery copy, not a second skill. A
hand-placed .md file with no library record is an
unvetted candidate: the discovery directory mixes
synced-trusted and unvetted files, and the hub must
not call the latter trusted merely because the file
exists.

Health is cached, timestamped, and never probed at
read time. Planning must not wait on probing every
MCP server or rerunning every skill test to build a
DAG. Maintenance checks (or any async writer) call
record_health(); index() merges the cached health
onto the live entries. A health record for a
capability that no longer exists is ignored at read
time and removable with prune_health().

Trust doctrine, held, not changed here: evidence
plus human approval promotes a capability. Success
counts inform; they never independently grant trust.
The counts-based lifecycle store is a manual
scoreboard and a registry-view field, not a planning
authority, and the hub does not read it as one.

Portability: the hub is home-relative and imports
only stdlib and the local store modules. It does not
depend on the WebUI server, on Modal, or on any
execution backend, so the same index serves a run
whether it executes locally or on Modal. Availability
records where a capability runs; the hub itself runs
wherever Aidon's home directory is.

A caller that already holds a loaded PluginRegistry
(a run's runner always does) should pass it in: loading
plugins executes their top-level code, and rebuilding
the registry inside the hub would execute it a second
time. A pre-loaded registry is reused as-is; without
one, the hub loads the registry itself, exactly as any
other standalone reader does.

NOT fully wired yet, deliberately: triage now reads the
hub (build_inventory), but planner skill recall still ranks
trusted first then validated then candidate, and no
writer syncs hub entries yet, because the hub owns
no store of record and index() rebuilds from the
live stores. Moving triage to the hub, restricting
ordinary recall to trusted skills, and connecting
writers are the integration steps, in that order.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

LIFECYCLES = ("builtin", "candidate", "validated", "trusted", "retired")
HEALTHS = ("healthy", "degraded", "unavailable", "unknown")
AVAILABILITIES = ("local", "remote", "disconnected")
VERIFICATIONS = ("untested", "passed", "failed")
PROVENANCES = ("built", "installed", "operator")
KINDS = ("tool", "plugin", "skill", "mcp", "image")

HEALTH_FILENAME = "capability_hub_health.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entry(entry_id, name, kind, lifecycle, availability, verification,
           provenance, source, tools, description, health="unknown",
           health_checked_at=None):
    return {
        "id": entry_id,
        "name": name,
        "kind": kind,
        "lifecycle": lifecycle,
        "health": health,
        "health_checked_at": health_checked_at,
        "availability": availability,
        "verification": verification,
        "permissions": _permissions_for(kind, availability),
        "provenance": provenance,
        "source": source,
        "tools": list(tools or []),
        "description": description or "",
    }


def _permissions_for(kind, availability):
    """Declared permissions only. In v1 the only declared
    permission is network for remote MCP servers; everything
    else declares nothing, which means undeclared, not safe."""
    if kind == "mcp" and availability == "remote":
        return ["network"]
    return []


def _mcp_availability(config) -> str:
    config = config if isinstance(config, dict) else {}
    transport = config.get("transport")
    if transport == "http" or config.get("url"):
        return "remote"
    if transport == "stdio" or config.get("command"):
        return "local"
    return "disconnected"


def _image_description(record) -> str:
    pip = ", ".join(record.get("pip", []) or [])
    apt = ", ".join(record.get("apt", []) or [])
    parts = []
    if pip:
        parts.append(f"pip: {pip}")
    if apt:
        parts.append(f"apt: {apt}")
    return "; ".join(parts) or "Modal capability image"


class CapabilityHub:
    """Local capability index over the existing stores."""

    def __init__(self, home, plugins=None):
        self.home = Path(home)
        self.plugins_registry = plugins

    def _health_path(self) -> Path:
        return self.home / HEALTH_FILENAME

    def _read_health(self) -> dict:
        path = self._health_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_health(self, health: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp_path = self._health_path().with_suffix(".tmp")
        tmp_path.write_text(json.dumps(health, indent=2, sort_keys=True))
        os.replace(tmp_path, self._health_path())

    def index(self) -> list:
        """Rebuild the full index from the live stores."""
        entries: dict[str, dict] = {}

        def put(entry):
            entries[entry["id"]] = entry

        self._add_builtin_tools(put)
        self._add_plugins(put)
        self._add_standalone_skills(put)
        self._add_library_skills(put)
        self._add_operator_mcp(put)
        self._add_image_manifests(put)
        self._add_registry(put)

        health = self._read_health()
        for entry in entries.values():
            record = health.get(entry["id"])
            if isinstance(record, dict) and record.get("health") in HEALTHS:
                entry["health"] = record["health"]
                entry["health_checked_at"] = record.get("checked_at")
                if record.get("availability") in AVAILABILITIES:
                    entry["availability"] = record["availability"]
                    entry["permissions"] = _permissions_for(
                        entry["kind"], entry["availability"])
        return sorted(entries.values(), key=lambda e: e["id"])

    def get(self, entry_id: str):
        for entry in self.index():
            if entry["id"] == entry_id:
                return entry
        return None

    def list(self, kind=None, lifecycle=None, health=None) -> list:
        entries = self.index()
        if kind is not None:
            entries = [e for e in entries if e["kind"] == kind]
        if lifecycle is not None:
            entries = [e for e in entries if e["lifecycle"] == lifecycle]
        if health is not None:
            entries = [e for e in entries if e["health"] == health]
        return entries

    def search(self, query: str) -> list:
        """Substring search over name, description, and tools,
        across all kinds: the discovery search the old
        tool-only search could not do."""
        needle = (query or "").lower()
        if not needle:
            return []
        hits = []
        for entry in self.index():
            haystack = " ".join([entry["name"], entry["description"],
                                 *entry["tools"]]).lower()
            if needle in haystack:
                hits.append(entry)
        return hits

    def trusted(self) -> list:
        """Entries a run may use without further approval:
        trusted capabilities plus the arena's builtin tools."""
        return [e for e in self.index()
                if e["lifecycle"] in ("trusted", "builtin")]

    def record_health(self, entry_id: str, health: str,
                      availability=None, note=None) -> dict:
        """Async health write: a maintenance check reports
        what it found; the hub never probes at read time."""
        if health not in HEALTHS:
            raise ValueError(f"unknown health: {health!r}")
        if availability is not None and availability not in AVAILABILITIES:
            raise ValueError(f"unknown availability: {availability!r}")
        if self.get(entry_id) is None:
            raise ValueError(f"unknown capability: {entry_id}")
        cache = self._read_health()
        record = {"health": health, "checked_at": _now()}
        if availability is not None:
            record["availability"] = availability
        if note is not None:
            record["note"] = note
        cache[entry_id] = record
        self._write_health(cache)
        return record

    def prune_health(self) -> int:
        """Drop health records for capabilities that no longer
        exist. Returns how many were removed."""
        live = {e["id"] for e in self.index()}
        cache = self._read_health()
        stale = [k for k in cache if k not in live]
        for key in stale:
            del cache[key]
        if stale:
            self._write_health(cache)
        return len(stale)

    def _add_builtin_tools(self, put) -> None:
        try:
            from .tool_catalog import GOAL_TOOL_NAMES, SANDBOX_TOOL_NAMES
        except Exception:
            return
        for name in (*SANDBOX_TOOL_NAMES, *GOAL_TOOL_NAMES):
            put(_entry(f"tool:{name}", name, "tool", "builtin", "local",
                       "passed", "installed", "builtin", [name],
                       "Arena tool"))

    def _load_plugins(self):
        """Plugin objects without re-executing plugin code.

        A pre-loaded registry is reused as-is. Otherwise the
        registry is loaded fresh, exactly as any standalone
        reader loads it. A stub without load_all yields nothing.
        """
        reg = self.plugins_registry
        if reg is not None:
            loaded = getattr(reg, "plugins", None)
            if isinstance(loaded, dict) and loaded:
                return list(loaded.values())
            loader = getattr(reg, "load_all", None)
            if callable(loader):
                try:
                    return loader()
                except Exception:
                    return []
            return []
        try:
            from .plugins import PluginRegistry
            return PluginRegistry(self.home).load_all()
        except Exception:
            return []

    def _add_plugins(self, put) -> None:
        plugins = self._load_plugins()
        for plugin in plugins:
            tools = [str(t.get("name", "")) for t in plugin.tools]
            put(_entry(f"plugin:{plugin.name}", plugin.name, "plugin",
                       "trusted", "local", "passed", "operator",
                       f"plugins/{plugin.name}", tools,
                       plugin.description))
            for skill in plugin.skills:
                skill_name = f"{plugin.name}/{skill}"
                put(_entry(f"skill:{skill_name}", skill_name, "skill",
                           "trusted", "local", "passed", "operator",
                           f"plugins/{plugin.name}/{skill}", [],
                           f"Skill bundled with plugin {plugin.name}"))

    def _add_standalone_skills(self, put) -> None:
        try:
            from .skill_library import SkillLibrary
            library_names = {s.name for s in SkillLibrary(self.home).list(
                states=("candidate", "validated", "trusted", "retired"))}
        except Exception:
            library_names = set()
        skills_dir = self.home / "skills"
        if not skills_dir.is_dir():
            return
        for path in sorted(skills_dir.glob("*.md")):
            if path.stem in library_names:
                continue
            text = ""
            try:
                text = path.read_text(encoding="utf-8")
            except Exception:
                pass
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip()), "")
            put(_entry(f"skill:{path.name}", path.name, "skill",
                       "candidate", "local", "untested", "operator",
                       f"skills/{path.name}", [],
                       first[:160]))

    def _add_library_skills(self, put) -> None:
        try:
            from .skill_library import SkillLibrary
            skills = SkillLibrary(self.home).list(
                states=("candidate", "validated", "trusted", "retired"))
        except Exception:
            return
        for skill in skills:
            put(_entry(f"skill:{skill.name}", skill.name, "skill",
                       skill.state, "local",
                       "passed" if skill.state in ("validated", "trusted")
                       else "untested",
                       "built", f"skills.json:{skill.name}", [],
                       skill.description))

    def _add_operator_mcp(self, put) -> None:
        try:
            data = json.loads((self.home / "mcp_servers.json").read_text())
        except Exception:
            return
        rows = []
        if isinstance(data, dict):
            rows = [(str(k), v) for k, v in data.items()]
        elif isinstance(data, list):
            rows = [(str(i.get("name", i)) if isinstance(i, dict) else str(i),
                     i if isinstance(i, dict) else {}) for i in data]
        for name, config in rows:
            config = config if isinstance(config, dict) else {}
            tools = config.get("tools")
            put(_entry(f"mcp:{name}", name, "mcp", "candidate",
                       _mcp_availability(config), "untested", "operator",
                       f"mcp_servers.json:{name}",
                       [str(t) for t in tools] if isinstance(tools, list)
                       else [],
                       f"MCP server (operator-configured)"))

    def _add_image_manifests(self, put) -> None:
        try:
            data = json.loads(
                (self.home / "capability_images.json").read_text())
        except Exception:
            return
        if not isinstance(data, dict):
            return
        for name, record in data.items():
            if not isinstance(record, dict):
                continue
            put(_entry(f"image:{name}", name, "image", "candidate",
                       "remote", "passed", "built",
                       f"capability_images.json:{name}", [],
                       _image_description(record)))

    def _add_registry(self, put) -> None:
        try:
            from .capability_registry import CapabilityRegistry
            entries = CapabilityRegistry(self.home).list()
        except Exception:
            return
        try:
            manifests = json.loads(
                (self.home / "capability_images.json").read_text())
            manifests = manifests if isinstance(manifests, dict) else {}
        except Exception:
            manifests = {}
        for record in entries:
            name = record.get("name", "")
            kind = record.get("kind", "")
            config = record.get("config", {}) or {}
            if kind == "mcp":
                tools = config.get("tools")
                put(_entry(f"mcp:{name}", name, "mcp",
                           record.get("status", "validated"),
                           _mcp_availability(config), "passed", "built",
                           f"capabilities.json:{name}",
                           [str(t) for t in tools]
                           if isinstance(tools, list) else [],
                           f"MCP server (registered)"))
            elif kind == "image":
                availability = ("remote" if name in manifests
                                else "disconnected")
                put(_entry(f"image:{name}", name, "image",
                           record.get("status", "validated"),
                           availability, "passed", "built",
                           f"capabilities.json:{name}", [],
                           _image_description(
                               manifests.get(name, config))))
