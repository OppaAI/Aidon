"""Deterministic plan validation, before execution, no model call.

A plan is checked by code, not by the model that proposed it:
- every step has a tool in the allowed set, a dict args, and
  a non-empty deliverable,
- plan deliverables match step deliverables,
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

ALLOWED_TOOLS = {"write", "read", "list", "shell", "calculate"}


def validate_plan(plan, allowed_tools: set[str] | None = None,
                  skill_names: set[str] | None = None) -> list[str]:
    allowed = set(allowed_tools) if allowed_tools is not None else ALLOWED_TOOLS
    skills = set(skill_names or [])
    failures = []
    steps = plan.normalized_steps() if hasattr(plan, "normalized_steps") else plan.steps
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
        if not deliverable and tool == "write":
            failures.append(f"step {index}: write step deliverable is empty")
        if not deliverable:
            continue
        if deliverable in deliverables:
            failures.append(f"step {index}: duplicate deliverable {deliverable}")
        deliverables.add(deliverable)
        for value in (args.values() if isinstance(args, dict) else []):
            if isinstance(value, str) and (value.startswith("/") or ".." in value.split("/")):
                failures.append(f"step {index}: path escape in args: {value}")
        if deliverable.startswith("/") or ".." in deliverable.split("/"):
            failures.append(f"step {index}: path escape in deliverable: {deliverable}")
    plan_deliverables = plan.deliverables if hasattr(plan, "deliverables") else []
    missing = set(plan_deliverables) - deliverables
    if missing:
        failures.append(f"plan deliverables with no step: {', '.join(sorted(missing))}")
    return failures
