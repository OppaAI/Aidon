"""Capability triage: before planning, check what the goal needs
against what Aidon actually has, and say so before execution.

This is the proactive step the reactive ladder lacks. The
execution-time lookup only fires after a plan already named a
tool that does not exist; triage runs before any plan exists,
so an impossible goal stops with a notification instead of
failing after three attempts, and a fillable gap reaches the
planner as context instead of an invented tool name.

Division of labour, deliberately:
- Inventory is the hub: tool names, skill names, and
  MCP server names, read from the one capability index
  (never invented). The run's own tool registries still
  contribute their executable tool names, because a tool
  registered at runtime but absent from the static
  catalog must still count; the hub contributes skills,
  MCP servers, and plugin tools.
- Triage reads the hub filtered: retired capabilities
  never count as coverage, and a capability whose health
  is positively unavailable does not count either.
  Skills go further: only trusted skills count, because
  triage coverage must not exceed what the planner can
  actually see — the plan prompt carries trusted skills
  only, so a goal "covered" by a candidate or validated
  skill would be a gap the planner cannot fill. MCP
  servers keep the looser rule: a validated server can
  still be connected with per-use approval, so it may
  count as coverage where a skill may not.
  Unknown health still counts: no check has reported
  anything, and an unchecked capability must not vanish
  from triage before the first health check runs.
  Retired registry servers and unavailable servers are
  the two cases the old assembly could not see.
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

MIN_CONFIDENCE = 0.6
BLOCK_CONFIDENCE = 0.8


def builtin_image_edit_covers(goal: str, tool_names) -> bool:
    """Avoid a Lab referral when image editing has a built-in tool.

    Whether an image was successfully attached is a separate input problem;
    it must not make an existing editing capability look unavailable.
    """
    if "edit_image" not in set(tool_names or ()):
        return False
    low = str(goal or "").lower()
    return any(term in low for term in (
        "edit", "modify", "transform", "retouch", "adjust", "enhance",
        "filter", "crop", "resize", "recolor", "remove background",
        "background removal", "inpaint", "outpaint",
    ))

STATUS_CRITERIA = {
    "covered": "Every capability the goal needs is in the inventory.",
    "need_skill": "A reusable procedure might improve this work, but no "
                  "tool, service connection, or executable action is missing.",
    "need_mcp": "An MCP server (approved or new) could cover a missing "
                "capability.",
    "need_plugin": "A new tool plugin, built and tested in the sandbox, "
                   "could cover a missing capability.",
    "blocked": "A required capability is missing and cannot be safely "
               "built, found, or connected.",
}

_NOTES = {
    "need_skill": (
        "No reusable skill is available yet. A skill is learned from a "
        "workflow after it succeeds; continue this goal with the actual "
        "tools and services available. Do not create or trust a skill "
        "before successful verification."),
    "need_mcp": (
        "Capability triage: this goal may need a capability not in "
        "the current inventory. Do not guess a vendor or connect. Ask "
        "the owner to continue in the MCP Connection Lab at "
        "[/labs](/labs) to compare real options and agree on the exact "
        "server before connection approval."),
    "need_plugin": (
        "Capability triage: this goal may need a capability not in "
        "the current inventory. Do not choose an implementation on the "
        "owner's behalf. Ask them to continue in the Tool Crafting Lab "
        "at [/labs](/labs) to define the tool before drafting or "
        "adoption approval."),
    "blocked": (
        "Capability check: this goal needs something Aidon does not "
        "have and cannot safely build, find, or connect, so I am "
        "stopping before planning rather than failing after attempts. "
        "If you can name the missing piece (a tool or MCP "
        "server), I can look for it, or we can narrow the goal."),
}


def build_inventory(home, tools, plugins, extra_tools) -> dict:
    """Inventory from the capability hub, plus the run's own
    executable tool names. The shape is unchanged
    ({tools, skills, mcp_servers}), so callers and the
    Clef prompt keep working; the source changed.

    Tools are the union of the runtime registries and the
    hub's plugin tools: the runtime is ground truth for
    what this run can execute. Skills and MCP servers come
    from the hub alone, filtered to live, usable entries:
    retired capabilities and positively-unavailable ones
    never count as coverage. Registry MCP servers, which
    the old file-only read could not see at all, now
    appear. A hub failure propagates: the loop's
    triage-skipped handler proceeds as covered, which is
    safer than triaging over a silently empty inventory
    that could wrongly block the run."""
    names: set[str] = set()
    try:
        names.update(str(n) for n in tools.names())
    except Exception:
        pass
    try:
        names.update(str(n) for n in plugins.tool_names())
    except Exception:
        pass
    try:
        names.update(str(n) for n in (extra_tools or {}))
    except Exception:
        pass
    from ..capabilities.capability_hub import CapabilityHub
    skills: list[dict] = []
    mcp: set[str] = set()
    for entry in CapabilityHub(home, plugins=plugins).index():
        if entry["lifecycle"] == "retired":
            continue
        if entry["health"] == "unavailable":
            continue
        if entry["kind"] == "skill":
            if entry["lifecycle"] != "trusted":
                continue
            skills.append({"name": entry["name"],
                           "description": entry["description"][:160]})
        elif entry["kind"] == "mcp":
            mcp.add(entry["name"])
        elif entry["kind"] == "plugin":
            names.update(entry["tools"])
    return {"tools": sorted(names),
            "skills": skills,
            "mcp_servers": sorted(mcp)}


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
