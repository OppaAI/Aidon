"""Plan-first guard: validate a plan before it runs.

A plan is a dict with a "steps" list (a "nodes" list is accepted as
an alias). Each step is a dict with id, tool, args, deliverable,
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


def validate_plan(plan: dict, available_tools) -> list[str]:
    """Return failure strings, empty when the plan is valid."""
    failures = []
    if not isinstance(plan, dict):
        return ["plan is not a dict"]
    steps = _steps(plan)
    if not steps:
        return ["plan has no steps"]
    tools = set(available_tools or [])
    ids = []
    deliverables = []
    for index, step in enumerate(steps):
        where = f"step {index}"
        if not isinstance(step, dict):
            failures.append(f"{where} is not a dict")
            continue
        step_id = step.get("id")
        if not step_id:
            failures.append(f"{where} has no id")
        else:
            ids.append(step_id)
        tool = step.get("tool")
        if not tool:
            failures.append(f"{where} ({step_id or '?'}) has no tool")
        elif tool not in tools:
            failures.append(f"{where} ({step_id}) uses unknown tool {tool!r}")
        args = step.get("args")
        if args is None:
            failures.append(f"{where} ({step_id}) has no args")
        elif not isinstance(args, dict):
            failures.append(f"{where} ({step_id}) args are not a dict")
        deliverable = step.get("deliverable")
        if not deliverable:
            failures.append(f"{where} ({step_id}) has no deliverable")
        else:
            deliverables.append(deliverable)
        depends = step.get("depends_on", [])
        if depends is not None and not isinstance(depends, list):
            failures.append(f"{where} ({step_id}) depends_on is not a list")
    seen = set()
    for deliverable in deliverables:
        if deliverable in seen:
            failures.append(f"duplicate deliverable {deliverable!r}")
        seen.add(deliverable)
    id_set = set(ids)
    for step in steps:
        if not isinstance(step, dict):
            continue
        for dep in step.get("depends_on", []) or []:
            if dep not in id_set:
                failures.append(
                    f"step {step.get('id')} depends on unknown step {dep!r}")
            elif dep == step.get("id"):
                failures.append(f"step {step.get('id')} depends on itself")
    # Cycle check: a step reachable from itself through depends_on.
    graph = {s.get("id"): list(s.get("depends_on", []) or [])
             for s in steps if isinstance(s, dict) and s.get("id")}
    visiting, visited = set(), set()

    def _walk(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited or node not in graph:
            return False
        visiting.add(node)
        try:
            return any(_walk(dep) for dep in graph[node])
        finally:
            visiting.discard(node)
            visited.add(node)

    for node in graph:
        if _walk(node):
            failures.append(f"cycle in depends_on involving step {node!r}")
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
