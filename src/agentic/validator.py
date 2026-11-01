"""Deterministic plan validation, before execution, no model call.

A plan is checked by code, not by the model that proposed it:
- every step has a tool in the allowed set and dict args;
- file-producing steps have a deliverable, while result-only steps may
  return their tool output directly to the user;
- plan deliverables match step deliverables,
- filenames the user explicitly introduced as "file named X" are preserved
  exactly in the plan's deliverables,
- no absolute path, no parent-directory escape, in any
  file argument or deliverable,
- no duplicate deliverable from two different tools, which
  would make verification ambiguous.
A plan that fails validation is not executed, it is a
repair candidate, with the failure list as the repair context.
This is the deterministic validation stage the demo plan
called for, in code, not as a model opinion.
"""

from __future__ import annotations

import re

ALLOWED_TOOLS = {"write", "read", "list", "shell", "calculate"}


def _explicit_named_files(goal: str) -> list[str]:
    """Return filenames the user explicitly introduces with ``file named``.

    These are task contracts, not suggestions from the planner. Keep this
    intentionally narrow so ordinary mentions of filenames remain flexible.
    """
    pattern = re.compile(
        r"\bfile\s+named\s+[`\"']?"
        r"([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)",
        re.IGNORECASE)
    found = []
    for match in pattern.finditer(str(goal or "")):
        name = match.group(1).strip(".`\"'")
        if name and name not in found:
            found.append(name)
    return found


def validate_plan(plan, allowed_tools: set[str] | None = None,
                  skill_names: set[str] | None = None,
                  goal: str = "") -> list[str]:
    allowed = set(allowed_tools) if allowed_tools is not None else ALLOWED_TOOLS
    skills = set(skill_names or [])
    failures = []
    steps = plan.normalized_steps() if hasattr(plan, "normalized_steps") else plan.steps
    if not steps:
        return ["plan has no steps"]
    deliverables = set()
    for index, step in enumerate(steps):
        tool = step.tool if hasattr(step, "tool") else step.get("tool", "")
        args = step.args if hasattr(step, "args") else step.get("args", {})
        deliverable = step.deliverable if hasattr(step, "deliverable") else step.get("deliverable", "")
        if tool not in allowed:
            if tool in skills:
                # The classic tool/skill confusion: the plan prompt
                # lists skills alongside tools, and a model puts a
                # skill name in the tool slot. Skills are guidance
                # text, never callable steps — name the confusion
                # so the repair pass fixes the right thing.
                failures.append(f"step {index}: {tool} is a skill, not a "
                                "callable tool; follow its text, do not "
                                "call it as a step tool")
            else:
                failures.append(f"step {index}: tool {tool} is not allowed")
        if not isinstance(args, dict):
            failures.append(f"step {index}: args must be a dict")
        elif tool in {"write", "write_file"}:
            if not any(str(args.get(key, "")).strip()
                       for key in ("file", "name", "path", "input_path", "filename")):
                failures.append(f"step {index}: {tool} needs a destination file")
        elif tool == "shell":
            command = args.get("command", args.get("cmd", []))
            if isinstance(command, str):
                command = command.strip()
            if not command or (isinstance(command, list) and not any(command)):
                failures.append(f"step {index}: shell needs a non-empty command")
        elif tool == "generate_document":
            if not str(args.get("title", "")).strip():
                failures.append(f"step {index}: generate_document needs a title")
            # The document tool derives a timestamped, safe filename when
            # the user did not specify one. Requiring a model-invented path
            # here caused otherwise valid document requests to fail before
            # execution and encouraged arbitrary ``workdir/`` names.
        if not deliverable and tool in {"write", "write_file", "generate_document"}:
            failures.append(f"step {index}: {tool} step deliverable is empty")
        for value in (args.values() if isinstance(args, dict) else []):
            if isinstance(value, str) and (value.startswith("/") or ".." in value.split("/")):
                failures.append(f"step {index}: path escape in args: {value}")
        if deliverable:
            if deliverable in deliverables:
                failures.append(f"step {index}: duplicate deliverable {deliverable}")
            deliverables.add(deliverable)
            if deliverable.startswith("/") or ".." in deliverable.split("/"):
                failures.append(f"step {index}: path escape in deliverable: {deliverable}")
    plan_deliverables = plan.deliverables if hasattr(plan, "deliverables") else []
    missing = set(plan_deliverables) - deliverables
    if missing:
        failures.append(f"plan deliverables with no step: {', '.join(sorted(missing))}")
    for required in _explicit_named_files(goal):
        if required not in plan_deliverables or required not in deliverables:
            failures.append(
                f"explicitly requested filename {required!r} must appear "
                "exactly in plan and step deliverables")
        if (re.search(r"\bexactly\s+one\s+(?:file|artifact)\b",
                      str(goal or ""), re.IGNORECASE)
                and set(str(name) for name in plan_deliverables) != {required}):
            failures.append(
                f"goal requires exactly one deliverable, {required!r}; "
                "remove unrelated deliverables")
    return failures
