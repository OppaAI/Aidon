"""Verified goal specification model and Markdown sheet parsing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


PLACEHOLDERS = ("tbd", "todo", "to be decided", "unknown", "n/a")


@dataclass
class Spec:
    goal: str = ""
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    budget_cents: int | None = None
    deadline: str = ""
    recipients: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    notifications: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "deliverables": self.deliverables,
            "criteria": self.criteria,
            "constraints": self.constraints,
            "budget_cents": self.budget_cents,
            "deadline": self.deadline,
            "recipients": self.recipients,
            "files": self.files,
            "notifications": self.notifications,
            "assumptions": self.assumptions,
            "open_questions": self.open_questions,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Spec":
        if not isinstance(data, dict):
            return cls()
        def as_list(value):
            if isinstance(value, list):
                return [str(v) for v in value]
            return [str(value)] if value else []
        budget = data.get("budget_cents")
        try:
            budget = int(budget) if budget is not None else None
        except (TypeError, ValueError):
            budget = None
        return cls(
            goal=str(data.get("goal", "")),
            deliverables=as_list(data.get("deliverables", [])),
            criteria=as_list(data.get("criteria", [])),
            constraints=as_list(data.get("constraints", [])),
            budget_cents=budget,
            deadline=str(data.get("deadline", "")),
            recipients=as_list(data.get("recipients", [])),
            files=as_list(data.get("files", [])),
            notifications=as_list(data.get("notifications", [])),
            assumptions=as_list(data.get("assumptions", [])),
            open_questions=as_list(data.get("open_questions", [])))

    def verify(self) -> tuple[bool, list[str]]:
        """Code checks for a complete, unambiguous spec."""
        failures: list[str] = []
        if not self.goal.strip() or self.goal.strip().lower() in PLACEHOLDERS:
            failures.append("goal is missing or a placeholder")
        if not self.deliverables:
            failures.append("deliverables are missing")
        for item in self.deliverables:
            if item.strip().lower() in PLACEHOLDERS:
                failures.append(f"deliverable is a placeholder: {item}")
        for item in self.criteria:
            if item.strip().lower() in PLACEHOLDERS:
                failures.append(f"done-when criterion is a placeholder: {item}")
        if self.budget_cents is not None and self.budget_cents < 0:
            failures.append("budget_cents is negative")
        if self.open_questions:
            failures.append(f"open questions remain: {', '.join(self.open_questions)}")
        for field_name, values in (("constraints", self.constraints),
                                   ("files", self.files)):
            for item in values:
                if item.strip().lower() in PLACEHOLDERS:
                    failures.append(f"{field_name} contains a placeholder: {item}")
        return (not failures, failures)


def spec_sheet(spec: Spec) -> str:
    """Readable spec sheet for the approval card, not raw JSON.

    The card body is this sheet: goal, deliverables,
    constraints, budget in dollars, deadline, recipients,
    files, notifications, assumptions, and open questions.
    A human approves a sheet they can read, not a JSON dump.
    """
    def lines(title: str, values: list[str]) -> list[str]:
        if not values:
            return [f"{title}: -"]
        out = [f"{title}:"]
        out.extend(f"  - {value}" for value in values)
        return out
    budget = "-"
    if spec.budget_cents is not None:
        budget = f"${spec.budget_cents / 100:.2f} ({spec.budget_cents} cents)"
    sheet = [f"Goal: {spec.goal}", f"Budget: {budget}",
             f"Deadline: {spec.deadline or '-'}"]
    sheet.extend(lines("Deliverables", spec.deliverables))
    sheet.extend(lines("Done when", spec.criteria))
    sheet.extend(lines("Constraints", spec.constraints))
    sheet.extend(lines("Recipients", spec.recipients))
    sheet.extend(lines("Files", spec.files))
    sheet.extend(lines("Notifications", spec.notifications))
    sheet.extend(lines("Assumptions", spec.assumptions))
    sheet.extend(lines("Open questions", spec.open_questions))
    return "\n".join(sheet)


def spec_sheet_md(spec: Spec) -> str:
    """Standard format spec sheet, Markdown, human readable.

    This is THE spec sheet format: Markdown headings and
    bullet lists, meant to be read by a human in an editor
    or in the chat box, not a JSON dump. It is the file
    users write themselves, copy, edit, and attach in
    chat, and it is the body of the spec sheet approval
    card. JSON is only the machine copy stored alongside
    it in specs/. The header points to the template and
    to the docs, so a user who finds a sheet in specs/
    knows they can write their own.
    """
    def bullets(values: list[str]) -> str:
        return "\n".join(f"- {v}" for v in values) if values else "None"
    budget = "None"
    if spec.budget_cents is not None:
        budget = f"${spec.budget_cents / 100:.2f} ({spec.budget_cents} cents)"
    return "\n".join([
        "# Spec Sheet",
        "",
        "How to use this sheet: fill it in, save it, and attach it in the chat box. "
        "Aidon reads it, verifies it, and skips the interview. "
        "You can also edit a frozen spec on the sheet itself in chat, save it, and proceed. "
        "Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo. "
        "Docs: README, Spec sheet section.",
        "",
        f"## Goal",
        "",
        spec.goal or "None",
        "",
        "## Done when",
        "",
        bullets(spec.criteria),
        "",
        "## Deliverables",
        "",
        bullets(spec.deliverables),
        "",
        "## Constraints",
        "",
        bullets(spec.constraints),
        "",
        f"## Budget",
        "",
        budget,
        "",
        f"## Deadline",
        "",
        spec.deadline or "No deadline",
        "",
        "## Recipients",
        "",
        bullets(spec.recipients),
        "",
        "## Files",
        "",
        bullets(spec.files),
        "",
        "## Notifications",
        "",
        bullets(spec.notifications),
        "",
        "## Assumptions",
        "",
        bullets(spec.assumptions),
        "",
        "## Open questions",
        "",
        bullets(spec.open_questions),
        "",
    ])


SPEC_SHEET_TEMPLATE = """# Spec Sheet

How to use this sheet: fill in the three required sections (Goal, Done when, Constraints). Everything else is optional. Blank means "use Aidon's configured defaults", and blank never triggers a question.
Save it and attach it in the chat box. Aidon reads it, verifies it, and skips the interview. You can also edit a frozen spec on the sheet itself in chat, save it, and proceed.

Prefill: Aidon may prefill non-critical fields from your past tasks and memory. Prefilled values are proposals, not facts: review them, edit them, or delete them. Aidon never prefills Budget, Recipients, or anything that sends, spends, or publishes.

Permissions rule: unspecified permissions follow Aidon's enforced policy; a blank field never grants additional authority. Actions that need approval stay gated even after the plan is approved. To restrict further, write it as a Constraint.

Template: specs/template.md in your Aidon home, and examples/SPEC-SHEET-TEMPLATE.md in the repo.
Example: examples/SPEC-SHEET-EXAMPLE.md in the repo.
Docs: README, Spec sheet section.

## Goal

Write the goal in plain language, in one sentence, saying what you want Aidon to do and for whom, not how it will do it. This section is required.

## Done when

Write checkable acceptance criteria, one per bullet. Aidon verifies each one before reporting done. Prefer things it can test (a command, a count, a total, a source URL) over opinions. This section is required.

## Constraints

List every hard limit Aidon must not break, one per bullet, such as a date range, a format, or a source file. Write None if there are no hard limits. This section is required.
Write scope, priorities, and permission restrictions here too, one per bullet, for example "Out of scope: ...", "Priority: accuracy over speed", or "Never: ...".

## Deliverables

List each file or concrete outcome Aidon must produce, one per bullet, with the file name if it is a file, and the properties it must have.

## Inputs and context

List every input file, link, repo, or dataset Aidon should read first, one per bullet, with what each is for. Say who the audience is and what tone to use if it matters.

## Budget

Write the maximum total amount Aidon may spend, in dollars and cents, or write None if there is no budget cap. Do not write a budget here unless it is a real limit. Write other limits (a time cap, a max step count) as Constraints.

## Deadline

Write the date and time the work must be finished by, or write No deadline if there is none.

## Recipients

List who the work is for or who should receive the output, with their contact details if Aidon must send it to them, or write None if there is no recipient.

## Files

List every output file Aidon should create, one per bullet, with the file name and path. Input files go under Inputs and context.

## Notifications

Write how you want to be told when the work is done or if it fails, or write None if no notification is needed. A checkpoint (pause for review after a phase) goes here as "Checkpoint: ...".

## Permissions

Aidon enforces its own policy; this table is the default, not a grant. Override only by writing a Constraint.

| Action | Level |
|---|---|
| Read files, browse, research | auto |
| Write or edit files in the workspace | auto |
| Run code, scripts, tests in the sandbox | auto |
| Install packages or tools | ask |
| Create and register new tools, skills, or MCP servers | ask |
| Send email or messages to other people | ask (draft first) |
| Spend real money or make purchases | never |
| Post or publish anything publicly | never |

## If something goes wrong

Aidon preserves successful outputs, diagnoses the failure, attempts a bounded repair, re-runs the affected checks and any necessary regression checks, records the failure and repair outcome, and stops and reports if the retry limit is reached or safe progress is no longer possible. To change this, write a bullet under Assumptions starting with "Recovery:".

## Assumptions

List anything Aidon may assume without asking, one per bullet, or write None if there are no assumptions. Aidon adds its own assumptions to the plan for you to confirm.

## Open questions

List any questions that are still open, one per bullet, or write None if the spec is complete. A spec with open questions is not verified, and Aidon asks only about the ones that block the work.

## What Aidon does with this sheet

1. Validate: checks that the required sections are filled and the criteria are testable.
2. Fill gaps: infers defaults and states each assumption instead of interviewing. It asks only when a missing answer would change the work or cause something irreversible.
3. Plan: writes a short plan covering steps, tools, assumptions, and how each "Done when" item will be checked.
4. Execute: works within permissions and limits, and keeps a run log of actions, spend, and decisions.
5. Verify: checks every "Done when" item and reports pass or fail with evidence.
6. Report: delivers the outputs and a short summary with the evidence and outcome for each acceptance check, estimates, unverified claims, limitations, and failed checks, the files created or modified and any new capabilities, and any outstanding approvals or actions not performed. Final status: Success, Partial success, or Failed.
"""


def _parse_budget(text: str) -> int | None:
    """Money only: cents, a $ amount, or a bare number alone.

    A line that merely contains a number ("Time cap: 60 min",
    "3 files") is not a budget and must not become one.
    """
    import re
    text = text.strip()
    if not text or text == "-":
        return None
    match = re.search(r"(\d+)\s*cents", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d+(?:\.\d{1,2})?)\s*dollars", text, re.IGNORECASE)
    if match:
        return int(round(float(match.group(1)) * 100))
    match = re.search(r"\$\s*(\d+(?:\.\d{1,2})?)", text)
    if match:
        return int(round(float(match.group(1)) * 100))
    if re.fullmatch(r"\d+(?:\.\d{1,2})?", text):
        return int(round(float(text) * 100))
    return None


def parse_spec_sheet(text: str) -> Spec:
    """Parse a user-written spec sheet back into a Spec.

    Accepts JSON, the plain spec_sheet() format, and the
    Markdown standard format, so a user can write a sheet
    in an editor, in chat, or by copying the template,
    and Aidon can read it without an interview. Unknown
    lines are ignored, never invented into fields.
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return Spec.from_dict(json.loads(stripped))
        except (json.JSONDecodeError, TypeError):
            pass
    spec = Spec()
    section = ""
    section_prefix = ""
    sections = {
        "goal": "goal", "done when": "criteria",
        "acceptance criteria": "criteria",
        "deliverables": "deliverables",
        "constraints": "constraints", "budget": "budget",
        "deadline": "deadline", "recipients": "recipients",
        "files": "files", "inputs": "files", "inputs and context": "files",
        "notifications": "notifications",
        "assumptions": "assumptions", "open questions": "open_questions",
        "priorities": ("constraints", "Priority: "),
        "out of scope": ("constraints", "Out of scope: "),
        "permissions": ("constraints", "Permission: "),
        "if something goes wrong": ("assumptions", "Recovery: "),
        "recovery": ("assumptions", "Recovery: "),
        "checkpoints and reporting": ("notifications", "Checkpoint: "),
        "checkpoints": ("notifications", "Checkpoint: "),
    }

    def _resolve(heading: str) -> tuple[str, str]:
        value = sections[heading]
        if isinstance(value, tuple):
            return value
        return value, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.lower() in ("none", "no deadline", "-"):
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip().lower()
            if heading in sections and heading != "spec sheet":
                section, section_prefix = _resolve(heading)
            continue
        if ":" in line and not line.startswith(("-", "*")):
            key, value = line.split(":", 1)
            key = key.strip().lower()
            if key in sections:
                section, section_prefix = _resolve(key)
                value = value.strip()
                if not value or value == "-":
                    continue
                if section == "goal":
                    spec.goal = value
                elif section == "budget":
                    spec.budget_cents = _parse_budget(value)
                elif section == "deadline":
                    spec.deadline = value
                else:
                    getattr(spec, section).append(section_prefix + value)
                continue
        if line.startswith(("-", "*")):
            item = line.lstrip("-*").strip()
            if not item or item == "-" or item.lower() == "none":
                continue
            if section == "goal":
                spec.goal = item
            elif section in ("deliverables", "criteria", "constraints",
                             "recipients", "files", "notifications",
                             "assumptions", "open_questions"):
                getattr(spec, section).append(section_prefix + item)
            continue
        # A bare line under Goal, Budget, or Deadline is that field.
        if section == "goal" and not spec.goal and line != "-":
            spec.goal = line
        elif section == "budget" and spec.budget_cents is None:
            spec.budget_cents = _parse_budget(line)
        elif section == "deadline" and not spec.deadline and line != "-":
            spec.deadline = line
    return spec


