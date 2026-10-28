"""Intake, Aidon intake concept, rebuilt, separate from interview.

Intake is the first step before an interview: take a raw message, extract a goal, a project
id, and any stated constraints, and validate that the goal is non-empty, so the interview
runner starts from a clean record, not from a raw chat line. No model call here, extraction
is by simple rules: project id from a leading #name, constraints from lines starting with
no, must, or only, and the goal is the message itself, trimmed.
"""

from __future__ import annotations

import re


def intake_from_message(message: str) -> dict:
    text = (message or "").strip()
    project_id = "default"
    match = re.match(r"#([A-Za-z0-9_-]+)\s+", text)
    if match:
        project_id = match.group(1)
        text = text[match.end():].strip()
    constraints = []
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith(("no ", "must ", "only ")):
            constraints.append(line.strip())
    return {"goal": text, "project_id": project_id, "constraints": constraints, "ok": bool(text)}


def validate_intake(intake: dict) -> list[str]:
    failures = []
    if not intake.get("goal"):
        failures.append("intake needs a goal")
    if not intake.get("project_id"):
        failures.append("intake needs a project_id")
    return failures
