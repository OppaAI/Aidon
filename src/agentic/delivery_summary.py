"""Delivery summary and problem narration, code only, no model call.

A delivery summary turns a run report into the Markdown a human
actually reads: what the goal was, which deliverables passed or
failed, which files were produced, how long it took, and how
many repairs it needed. Problem narration does the same for a
blocked run: what blocked it, and what was already tried, in
plain language, from the report's own fields.
"""

from __future__ import annotations


def _deliverable_rows(deliverables) -> list[tuple[str, str]]:
    rows = []
    for item in deliverables or []:
        if isinstance(item, dict):
            name = str(item.get("name", item.get("deliverable", "?")))
            status = item.get("status", "pass" if item.get("passed", True) else "fail")
            rows.append((name, str(status)))
        else:
            rows.append((str(item), "pass"))
    return rows


def delivery_summary(report: dict) -> str:
    """Render a run report dict as Markdown."""
    report = report or {}
    goal = report.get("goal", "(no goal)")
    lines = [f"# Delivery summary", "", f"**Goal:** {goal}", ""]
    rows = _deliverable_rows(report.get("deliverables"))
    if rows:
        lines.append("## Deliverables")
        lines.append("")
        for name, status in rows:
            lines.append(f"- {name}: {status}")
        lines.append("")
    files = report.get("files", report.get("files_produced", []))
    if files:
        lines.append("## Files produced")
        lines.append("")
        for path in files:
            lines.append(f"- {path}")
        lines.append("")
    elapsed = report.get("elapsed", report.get("elapsed_seconds"))
    if elapsed is not None:
        lines.append(f"**Elapsed:** {elapsed}")
        lines.append("")
    repairs = report.get("repairs")
    if repairs is not None:
        if isinstance(repairs, list):
            lines.append(f"**Repairs:** {len(repairs)}")
        else:
            lines.append(f"**Repairs:** {repairs}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def problem_narration(problem: str, context: dict | None = None) -> str:
    """Narrate a blocked run: what blocked it, what was tried, what is next.

    context may carry blocked_on, tried (a list of attempts),
    and next_step. Nothing is invented: fields that are absent
    are simply not narrated.
    """
    context = context or {}
    lines = [f"The run is blocked: {problem}."]
    blocked_on = context.get("blocked_on")
    if blocked_on:
        lines.append(f"It stopped at: {blocked_on}.")
    tried = context.get("tried", [])
    if tried:
        lines.append("Already tried: " + "; ".join(str(t) for t in tried) + ".")
    next_step = context.get("next_step")
    if next_step:
        lines.append(f"Next step: {next_step}.")
    return " ".join(lines)
