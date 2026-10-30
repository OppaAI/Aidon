"""Capability triage: before planning, check what the goal needs
against what Aidon actually has, and say so before execution.

This is the proactive step the reactive ladder lacks. The
execution-time lookup only fires after a plan already named a
tool that does not exist; triage runs before any plan exists,
so an impossible goal stops with a notification instead of
failing after three attempts, and a fillable gap reaches the
planner as context instead of an invented tool name.

Division of labour, deliberately:
- Inventory is code: tool names, skill names, and approved MCP
  server names, read from the live sources (never invented).
- Gap classification is Clef: one choice question over
  covered / need_skill / need_mcp / need_plugin / blocked.
  Clef cannot NAME the missing capability (it returns only
  probabilities over given options), so triage notes are
  status-level, not specific: the planner identifies the
  specific gap from the note plus the goal. Naming gaps with
  a generative call first is the follow-up, not this step.
- Confidence is the safety valve: an uncertain triage never
  blocks. Below 0.6 the run proceeds as if covered; a
  low-confidence block (below 0.8) downgrades to need_plugin
  (climb, don't refuse). Only a high-confidence block stops
  the run, and only before planning, never mid-execution.
"""

from __future__ import annotations

import json
from pathlib import Path

MIN_CONFIDENCE = 0.6
BLOCK_CONFIDENCE = 0.8

STATUS_CRITERIA = {
    "covered": "Every capability the goal needs is in the inventory.",
    "need_skill": "A skill in the library, or a new skill, could cover "
                  "a missing capability.",
    "need_mcp": "An MCP server (approved or new) could cover a missing "
                "capability.",
    "need_plugin": "A new tool plugin, built and tested in the sandbox, "
                   "could cover a missing capability.",
    "blocked": "A required capability is missing and cannot be safely "
               "built, found, or connected.",
}

_NOTES = {
    "need_skill": (
        "Capability triage: this goal may need a capability not in "
        "the current inventory. A skill may cover it — look for or "
        "propose one before assuming the tool exists."),
    "need_mcp": (
        "Capability triage: this goal may need a capability not in "
        "the current inventory. An MCP server may cover it — propose "
        "a connection (approved servers reconnect; a new server needs "
        "the user's approval with its exact command or URL) before "
        "assuming it exists."),
    "need_plugin": (
        "Capability triage: this goal may need a capability not in "
        "the current inventory. A new tool plugin may be needed — "
        "draft it, sandbox-test it, and propose it for approval "
        "instead of calling a tool that does not exist."),
    "blocked": (
        "Capability check: this goal needs something Aidon does not "
        "have and cannot safely build, find, or connect, so I am "
        "stopping before planning rather than failing after attempts. "
        "If you can name the missing piece (a tool, skill, or MCP "
        "server), I can look for it, or we can narrow the goal."),
}


def _read_mcp_servers(home) -> list[str]:
    try:
        data = json.loads((Path(home) / "mcp_servers.json").read_text())
    except Exception:
        return []
    if isinstance(data, dict):
        return [str(k) for k in data]
    if isinstance(data, list):
        return [str(item.get("name", item)) if isinstance(item, dict)
                else str(item) for item in data]
    return []


def build_inventory(home, tools, plugins, extra_tools) -> dict:
    """Live inventory rows. Every source is guarded: a missing
    source gives an empty section, never a placeholder."""
    names: list[str] = []
    try:
        names.extend(str(n) for n in tools.names())
    except Exception:
        pass
    try:
        names.extend(str(n) for n in plugins.tool_names())
    except Exception:
        pass
    try:
        names.extend(str(n) for n in (extra_tools or {}))
    except Exception:
        pass
    skills: list[dict] = []
    try:
        for name, text in (plugins.skill_texts() or {}).items():
            skills.append({"name": str(name),
                           "description": str(text or "")[:160]})
    except Exception:
        pass
    return {"tools": sorted(set(names)),
            "skills": skills,
            "mcp_servers": _read_mcp_servers(home)}


def inventory_text(inventory: dict) -> str:
    parts = ["Tools: " + ", ".join(inventory.get("tools", []))]
    skills = inventory.get("skills", [])
    if skills:
        parts.append("Skills: " + "; ".join(
            f"{s['name']} — {s['description']}" for s in skills))
    else:
        parts.append("Skills: none")
    mcp = inventory.get("mcp_servers", [])
    parts.append("MCP servers: " + (", ".join(mcp) if mcp else "none"))
    return "\n".join(parts)


def triage(goal: str, inventory: dict, client, spec_text: str = "") -> dict:
    """One Clef choice -> {status, confidence, note}.

    client None means triage skipped: covered, so the run
    proceeds exactly as before. A transport or parse failure
    raises, and the caller treats it as skipped (proceed).
    """
    if client is None:
        return {"status": "covered", "confidence": 0.0, "note": ""}
    state = {"goal": goal, "inventory": inventory_text(inventory)}
    if spec_text:
        state["spec"] = spec_text[:1000]
    choice, confidence, _probs = client.decide_choice(
        state, "capability_status",
        "Given the goal and the listed tools, skills, and MCP servers, "
        "what is the capability status?",
        STATUS_CRITERIA)
    if confidence < MIN_CONFIDENCE:
        return {"status": "covered", "confidence": confidence, "note": ""}
    if choice == "blocked" and confidence < BLOCK_CONFIDENCE:
        # Uncertain block: climb instead of refusing. The planner
        # gets the plugin note and can still prove coverage.
        return {"status": "need_plugin", "confidence": confidence,
                "note": _NOTES["need_plugin"]}
    return {"status": choice, "confidence": confidence,
            "note": _NOTES.get(choice, "")}
