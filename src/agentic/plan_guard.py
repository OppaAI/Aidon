"""Plan-first guard: validate a plan before it runs.

A plan is a dict with a "steps" list (a "nodes" list is accepted as
an alias). Each step is a dict with id, tool, args, and an optional deliverable,
and depends_on (a list of step ids that must run first). The guard
checks the plan the way a runner would fail on it, so a bad plan
is rejected up front instead of halfway through execution.
"""

from __future__ import annotations


def _steps(plan: dict) -> list[dict]:
    if not isinstance(plan, dict):
        return []
    steps = plan.get("steps", plan.get("nodes", []))
    return steps if isinstance(steps, list) else []


def validate_plan(plan: dict, available_tools, skill_names=None) -> list[str]:
    """Return failure strings, empty when the plan is valid."""
    failures = []
    if not isinstance(plan, dict):
        return ["plan is not a dict"]
    steps = _steps(plan)
    if not steps:
        return ["plan has no steps"]
    tools = set(available_tools or [])
    skills = set(skill_names or [])
    graph = {}
    seen_deliverables = set()
    for index, step in enumerate(steps):
        where = f"step {index}"
        if not isinstance(step, dict):
            failures.append(f"{where} is not a dict")
            continue
        step_id = step.get("id")
        valid_id = isinstance(step_id, str) and bool(step_id.strip())
        if not valid_id:
            failures.append(f"{where} has no id or id is not a string")
        elif step_id in graph:
            failures.append(f"duplicate step id {step_id!r}")
        tool = step.get("tool")
        if not isinstance(tool, str) or not tool:
            failures.append(f"{where} ({step_id or '?'}) has no tool or tool is not a string")
        elif tool not in tools:
            if tool in skills:
                failures.append(f"{where} ({step_id}) uses skill {tool!r} "
                                "as a tool; skills are guidance text, "
                                "not callable steps")
            else:
                failures.append(f"{where} ({step_id}) uses unknown tool {tool!r}")
        args = step.get("args")
        if args is None:
            failures.append(f"{where} ({step_id}) has no args")
        elif not isinstance(args, dict):
            failures.append(f"{where} ({step_id}) args are not a dict")
        deliverable = step.get("deliverable")
        if deliverable not in (None, "") and not isinstance(deliverable, str):
            failures.append(f"{where} deliverable is not a string")
        elif deliverable:
            if deliverable in seen_deliverables:
                failures.append(f"duplicate deliverable {deliverable!r}")
            seen_deliverables.add(deliverable)
        elif isinstance(tool, str) and tool in {"write", "write_file", "generate_document"}:
            failures.append(f"{where} ({step_id}) has no deliverable")
        depends = step.get("depends_on")
        if depends is None:
            depends = []
        if not isinstance(depends, list):
            failures.append(f"{where} ({step_id}) depends_on is not a list")
            depends = []
        valid_depends = []
        for dep in depends:
            if not isinstance(dep, str) or not dep:
                failures.append(f"{where} ({step_id}) dependency is not a step id")
            else:
                valid_depends.append(dep)
        if valid_id:
            graph[step_id] = valid_depends
    for step_id, depends in graph.items():
        for dep in depends:
            if dep not in graph:
                failures.append(f"step {step_id} depends on unknown step {dep!r}")
            elif dep == step_id:
                failures.append(f"step {step_id} depends on itself")
    # Iterative topological validation also handles long, valid DAGs without
    # overflowing Python's recursion limit on model-supplied input.
    from collections import deque
    dependents = {step_id: [] for step_id in graph}
    counts = {step_id: 0 for step_id in graph}
    for step_id, depends in graph.items():
        for dep in depends:
            if dep in graph:
                dependents[dep].append(step_id)
                counts[step_id] += 1
    ready = deque(step_id for step_id, count in counts.items() if count == 0)
    while ready:
        for child in dependents[ready.popleft()]:
            counts[child] -= 1
            if counts[child] == 0:
                ready.append(child)
    for step_id, count in counts.items():
        if count:
            failures.append(f"cycle in depends_on involving step {step_id!r}")
            break
    return failures


def guard_plan(plan: dict, available_tools) -> dict:
    """Raise ValueError on failures, else return the plan unchanged."""
    failures = validate_plan(plan, available_tools)
    if failures:
        raise ValueError("plan guard rejected the plan: " + "; ".join(failures))
    return plan


def plan_first_summary(plan: dict) -> str:
    """One paragraph describing a plan: goal, step count, tools, deliverables."""
    steps = _steps(plan)
    tools = sorted({str(s.get("tool")) for s in steps
                    if isinstance(s, dict) and s.get("tool")})
    deliverables = [str(s.get("deliverable")) for s in steps
                    if isinstance(s, dict) and s.get("deliverable")]
    goal = plan.get("goal", "") if isinstance(plan, dict) else ""
    parts = []
    if goal:
        parts.append(f"Plan for {goal!r}")
    parts.append(f"with {len(steps)} step(s)")
    if tools:
        parts.append(f"using {', '.join(tools)}")
    if deliverables:
        parts.append(f"producing {', '.join(deliverables)}")
    return " ".join(parts) + "."
