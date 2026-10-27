"""Planner, old Aidon planner concept, rebuilt, deterministic, no model call.

plan_goal builds a plan from a goal, a pattern, and available tools: if a pattern matches,
its steps are used, otherwise a single write step is used, with the goal as deliverable
context. The plan is validated by the DAG validator before it is returned, so a planner
output that fails validation is not handed to the loop as if it were valid.
"""

from __future__ import annotations

from .dag import DAG, DAGNode
from .patterns import match_pattern


def plan_goal(goal: str, tools: list | None = None) -> dict:
    pattern = match_pattern(goal)
    if pattern:
        steps = [{"tool": step.get("tool", "write"), "args": {"goal": goal}, "deliverable": step.get("deliverable", "output"), "assumptions": []} for step in pattern["steps"]]
    else:
        steps = [{"tool": "write", "args": {"goal": goal}, "deliverable": "output", "assumptions": []}]
    nodes = []
    for index, step in enumerate(steps):
        node = {"id": f"step_{index}", "tool": step["tool"], "args": step["args"], "deliverable": step["deliverable"], "assumptions": step["assumptions"], "depends_on": [f"step_{index-1}"] if index else []}
        nodes.append(node)
    dag = DAG([DAGNode.from_dict(node) for node in nodes])
    failures = dag.validate()
    return {"goal": goal, "pattern": pattern["name"] if pattern else None, "nodes": nodes, "valid": not failures, "failures": failures}
