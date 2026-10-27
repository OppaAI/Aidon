"""Project brief, old Aidon project_brief concept, rebuilt.

A brief is the one-page record of a project before planning: goal, audience, constraints,
deliverables, and criteria, saved as JSON and Markdown under <home>/briefs/<project>.json/.md.
Building a brief is code only, no model call: the caller supplies the fields, this module
validates that goal, deliverables, and criteria are non-empty, and writes both files, so the
brief can be reviewed, frozen by MissionStore, or fed to the interview spec step.
"""

from __future__ import annotations

import json
from pathlib import Path


def validate_brief(brief: dict) -> list[str]:
    if not isinstance(brief, dict):
        return ["brief must be a dict"]
    failures = []
    if not brief.get("goal"):
        failures.append("brief needs a goal")
    if not isinstance(brief.get("deliverables"), list) or not brief["deliverables"]:
        failures.append("brief needs a non-empty deliverables list")
    if not isinstance(brief.get("criteria"), list) or not brief["criteria"]:
        failures.append("brief needs a non-empty criteria list")
    return failures


def brief_to_markdown(brief: dict) -> str:
    lines = [f"# Brief: {brief.get('project_id', 'project')}", "", f"Goal: {brief.get('goal', '')}", ""]
    lines.append("## Audience")
    lines.append(brief.get("audience", ""))
    lines.append("")
    lines.append("## Constraints")
    for item in brief.get("constraints", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Deliverables")
    for item in brief.get("deliverables", []):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Criteria")
    for item in brief.get("criteria", []):
        lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


def save_brief(home, project_id: str, brief: dict) -> dict:
    failures = validate_brief(brief)
    if failures:
        return {"ok": False, "failures": failures}
    data = {**brief, "project_id": project_id}
    directory = Path(home) / "briefs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{project_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    (directory / f"{project_id}.md").write_text(brief_to_markdown(data), encoding="utf-8")
    return {"ok": True, "json": str(directory / f"{project_id}.json"), "md": str(directory / f"{project_id}.md")}


def load_brief(home, project_id: str) -> dict | None:
    path = Path(home) / "briefs" / f"{project_id}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
