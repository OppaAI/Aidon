"""Capability lookup: one view over what Aidon can already do.

The sources of truth stay where they are — the live tool
registry, the skill library, and the approved-MCP list. This
module only searches them, in capability-ladder order, when
a plan or a DAG node names a tool that does not exist:

    exact/equivalent tool  ->  relevant skill  ->  approved MCP server

It returns structured matches the runner turns into a
binding re-plan instruction. It is deliberately a view,
not a second registry: nothing is stored here, so it can
never drift from the live toolset.
"""

from __future__ import annotations

import difflib
import re

_TOKEN = re.compile(r"[a-z0-9]+")
_CLOSE_RATIO = 0.72


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _field(item, key: str, default=""):
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _as_tool_rows(tools) -> list[tuple[str, str]]:
    """Normalise tools to (name, description) rows.

    Accepts a {name: description} dict, a list of dicts with
    name/description keys, or a list of objects with those
    attributes.
    """
    if tools is None:
        return []
    if isinstance(tools, dict):
        return [(str(k), str(v or "")) for k, v in tools.items()]
    rows = []
    for item in tools:
        rows.append((str(_field(item, "name", "")),
                     str(_field(item, "description", "") or "")))
    return rows


def _as_skill_rows(skills) -> list[dict]:
    if skills is None:
        return []
    rows = []
    for item in skills:
        rows.append({
            "name": str(_field(item, "name", "")),
            "description": str(_field(item, "description", "") or ""),
            "capabilities": list(_field(item, "capabilities", []) or []),
        })
    return rows


def _as_mcp_rows(mcp_servers) -> list[str]:
    if mcp_servers is None:
        return []
    if isinstance(mcp_servers, dict):
        return [str(k) for k in mcp_servers]
    return [str(_field(item, "name", item)) for item in mcp_servers]


def lookup_capability(name: str, *, tools=None, skills=None,
                      mcp_servers=None) -> list[dict]:
    """Find existing capabilities related to a missing tool `name`.

    Returns matches in ladder order: tools first (exact, then
    close name, then token overlap with the description), then
    skills, then MCP servers. Each match is a dict with
    {kind, name, detail}, at most 5 in total.
    """
    want = _norm(name)
    want_tokens = _tokens(name.replace("_", " "))
    matches: list[dict] = []

    # -- tools ------------------------------------------------------
    close: list[tuple[float, str, str]] = []
    for tname, desc in _as_tool_rows(tools):
        if not tname:
            continue
        if _norm(tname) == want:
            matches.append({"kind": "tool", "name": tname,
                            "detail": "exact name match"})
            continue
        ratio = difflib.SequenceMatcher(None, want, _norm(tname)).ratio()
        overlap = len(want_tokens & _tokens(tname.replace("_", " ")))
        desc_overlap = len(want_tokens & _tokens(desc))
        if ratio >= _CLOSE_RATIO or overlap >= 2 or (
                overlap >= 1 and desc_overlap >= 1):
            close.append((-(ratio + 0.2 * overlap + 0.1 * desc_overlap),
                          tname, desc))
    for _score, tname, desc in sorted(close)[:3]:
        matches.append({"kind": "tool", "name": tname,
                        "detail": (desc or "")[:160]})

    # -- skills -----------------------------------------------------
    for skill in _as_skill_rows(skills):
        sname = skill["name"]
        if not sname:
            continue
        hay = _tokens(sname.replace("_", " ")) | _tokens(skill["description"])
        for cap in skill["capabilities"]:
            hay |= _tokens(str(cap).replace("-", " "))
        overlap = len(want_tokens & hay)
        cap_hit = any(_norm(c) == want for c in skill["capabilities"])
        if cap_hit or overlap >= 2:
            matches.append({"kind": "skill", "name": sname,
                            "detail": (skill["description"] or "")[:160]})

    # -- approved MCP servers ---------------------------------------
    for server in _as_mcp_rows(mcp_servers):
        if not server:
            continue
        stoks = _tokens(server.replace("_", " ").replace("-", " "))
        overlap = len(want_tokens & stoks)
        substr = _norm(server) in want or want in _norm(server)
        if want_tokens and (substr or overlap >= max(1, len(want_tokens))):
            matches.append({"kind": "mcp", "name": server,
                            "detail": "approved MCP server — reconnect "
                                      "with mcp_connect"})

    order = {"tool": 0, "skill": 1, "mcp": 2}
    matches.sort(key=lambda m: order.get(m["kind"], 9))
    return matches[:5]


def recovery_note(tool_name: str, matches: list[dict]) -> str:
    """The binding instruction a re-plan gets for a missing tool."""
    head = (f"[missing capability] The plan referenced tool "
            f"{tool_name!r}, which is NOT registered. Never call it "
            f"again in this run.")
    if matches:
        lines = []
        for match in matches:
            if match["kind"] == "tool":
                lines.append(
                    f"- Use the existing tool {match['name']!r} instead "
                    f"({match['detail']}).")
            elif match["kind"] == "skill":
                lines.append(
                    f"- Follow the skill {match['name']!r} "
                    f"({match['detail']}) — it covers this capability "
                    f"with registered tools.")
            elif match["kind"] == "mcp":
                lines.append(
                    f"- First step: mcp_connect(name={match['name']!r}) "
                    f"to reconnect the approved MCP server, then use "
                    f"its tools.")
        return head + "\nExisting capabilities that cover this:\n" + \
            "\n".join(lines)
    return (head + "\nNo existing tool, skill, or approved MCP server "
            "covers it. Do not guess which service or implementation "
            "the owner wants, and do not start writing or connecting "
            "one yet. Tell the owner this capability is missing and "
            "direct them to the Capability Labs at [/labs](/labs), where they "
            "can choose tool crafting, skill learning, or MCP and work "
            "through the requirements before approving a brief. Resume "
            "the goal only after the owner has chosen and approved the "
            "needed capability.")


def capabilities_overview(*, tools, skills, mcp_servers, dags,
                          kernel_mode: bool = False) -> dict:
    """Assemble the capabilities-page view from live rows.

    Pure assembly over rows the caller gathered from the
    sources of truth (tool registry, skill library,
    approved-MCP list, DAG library). No row is invented here:
    an empty source gives an empty section, not a placeholder.
    """
    tool_rows = [{"name": n, "description": d}
                 for n, d in _as_tool_rows(tools)]
    skill_rows = _as_skill_rows(skills)
    mcp_rows = [{"name": n} for n in _as_mcp_rows(mcp_servers)]
    dag_rows = []
    if dags is not None:
        if isinstance(dags, dict):
            dag_rows = [{"id": str(k), **(v if isinstance(v, dict) else {})}
                        for k, v in dags.items()]
        else:
            dag_rows = [{"id": str(_field(d, "id", "")),
                         "goal": str(_field(d, "goal", "") or ""),
                         "nodes": _field(d, "nodes", 0)}
                        for d in dags]
    return {
        "tools": tool_rows,
        "skills": skill_rows,
        "mcp_servers": mcp_rows,
        "dags": dag_rows,
        "kernel_mode": bool(kernel_mode),
        "counts": {
            "tools": len(tool_rows),
            "skills": len(skill_rows),
            "mcp_servers": len(mcp_rows),
            "dags": len(dag_rows),
        },
    }
