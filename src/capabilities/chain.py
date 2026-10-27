"""Declarative tool chains, zero-LLM scheduled jobs, old Aidon chain concept, rebuilt.

A chain is a JSON file with steps, each step has a tool name and args, and a deliverable.
Chains are validated by code, never by the model, before they run: unknown tools are rejected,
duplicate deliverables are rejected, and steps must be a list with tool and deliverable.
Running a chain is sequential, through the sandbox ToolRegistry, not through the model,
so a chain costs no model calls, which is why it is useful for scheduled jobs.
A chain that fails validation is not run, and the failures are returned, not hidden.
"""

from __future__ import annotations

import json
from pathlib import Path

ALLOWED_CHAIN_TOOLS = ("write", "read", "list", "shell", "calculate")


def validate_chain_spec(spec: dict) -> list[str]:
    """Validate a chain spec dict, return failures, empty means valid."""
    if not isinstance(spec, dict):
        return ["chain spec must be a dict"]
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["chain spec needs a non-empty steps list"]
    failures = []
    deliverables = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            failures.append(f"step {index} must be a dict"); continue
        tool = step.get("tool", "")
        if tool not in ALLOWED_CHAIN_TOOLS:
            failures.append(f"step {index} unknown tool: {tool}")
        if not step.get("deliverable"):
            failures.append(f"step {index} needs a deliverable")
        elif step["deliverable"] in deliverables:
            failures.append(f"step {index} duplicate deliverable: {step['deliverable']}")
        else:
            deliverables.append(step["deliverable"])
    return failures


def load_chain_spec(path) -> dict:
    """Load a chain spec JSON file, return the dict, raise on unreadable or invalid JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("chain spec JSON must be an object")
    return data


def run_chain(spec: dict, tool_registry) -> dict:
    """Run a validated chain through a ToolRegistry, return executed steps or failures, never run an invalid chain."""
    failures = validate_chain_spec(spec)
    if failures:
        return {"ok": False, "failures": failures, "executed": []}
    executed = []
    for step in spec["steps"]:
        try:
            result = tool_registry.call(step["tool"], step.get("args", {}))
        except Exception as exc:
            return {"ok": False, "failures": [f"step {step.get('deliverable')} failed: {type(exc).__name__}: {exc}"], "executed": executed}
        executed.append({"tool": step["tool"], "deliverable": step["deliverable"], "result": result})
    return {"ok": True, "executed": executed}
