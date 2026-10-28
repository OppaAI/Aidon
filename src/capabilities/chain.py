"""Declarative tool chains, zero-LLM scheduled jobs.

A chain is a list of steps executed with zero LLM involvement — the
compiled form of "when X every Y, if <condition>, do <actions>". The
agent compiles the user's sentence into this spec once; the scheduler
then runs it mechanically on every firing.

Two supported shapes (both validated by code, never by the model):

1. Sequential sandbox shape (original new-Aidon form):

    {
      "steps": [
        {"tool": "write", "args": {"file": "a.txt", "text": "hi"},
         "deliverable": "a.txt"},
        {"tool": "read", "args": {"file": "a.txt"}, "deliverable": "read_a"}
      ]
    }

2. Conditional shape (restored from old Aidon / AAA):

    [
      {"tool": "read", "args": {"file": "status.txt"}, "as": "status"},
      {"if": {"field": "status", "op": "contains", "value": "ready"},
       "then": [
         {"tool": "write", "args": {"file": "go.txt", "text": "ok: {status}"}}
       ],
       "else": [
         {"tool": "shell", "args": {"cmd": "echo waiting"}}
       ]}
    ]

Condition shape is data, never code: {"field", "op", "value"}.
Supported ops: ==, !=, >, >=, <, <=, in, not_in, contains.
String args support {dotted.path} substitution from bound outputs.
Unknown paths are left as-is so a misnamed field is visible.
Nesting is bounded so a hand-edited pathological record cannot
blow the stack. One bad step is recorded and skipped; it never
aborts the rest of the chain.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

ALLOWED_CHAIN_TOOLS = ("write", "read", "list", "shell", "calculate")

_CONDITION_OPS = {"==", "!=", ">", ">=", "<", "<=", "in", "not_in", "contains"}
_MAX_NESTING = 10
_TEMPLATE_RE = re.compile(
    r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*)\}"
)


# ── Sequential (original) path ───────────────────────────────────────────────

def validate_chain_spec(spec: dict) -> list[str]:
    """Validate a sequential chain spec dict; empty means valid."""
    if not isinstance(spec, dict):
        return ["chain spec must be a dict"]
    steps = spec.get("steps")
    if not isinstance(steps, list) or not steps:
        return ["chain spec needs a non-empty steps list"]
    failures: list[str] = []
    deliverables: list[str] = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            failures.append(f"step {index} must be a dict")
            continue
        tool = step.get("tool", "")
        if tool not in ALLOWED_CHAIN_TOOLS:
            failures.append(f"step {index} unknown tool: {tool}")
        if not step.get("deliverable"):
            failures.append(f"step {index} needs a deliverable")
        elif step["deliverable"] in deliverables:
            failures.append(
                f"step {index} duplicate deliverable: {step['deliverable']}"
            )
        else:
            deliverables.append(step["deliverable"])
    return failures


def load_chain_spec(path) -> dict:
    """Load a chain spec JSON file; raise on unreadable or invalid JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("chain spec JSON must be an object")
    return data


def run_chain(spec: dict, tool_registry) -> dict:
    """Run a validated sequential chain through a ToolRegistry.

    Never runs an invalid chain. Returns executed steps or failures.
    """
    failures = validate_chain_spec(spec)
    if failures:
        return {"ok": False, "failures": failures, "executed": []}
    executed = []
    for step in spec["steps"]:
        try:
            result = tool_registry.call(step["tool"], step.get("args", {}))
        except Exception as exc:
            return {
                "ok": False,
                "failures": [
                    f"step {step.get('deliverable')} failed: "
                    f"{type(exc).__name__}: {exc}"
                ],
                "executed": executed,
            }
        executed.append({
            "tool": step["tool"],
            "deliverable": step["deliverable"],
            "result": result,
        })
    return {"ok": True, "executed": executed}


# ── Conditional path (restored from AAA) ─────────────────────────────────────

def _coerce_output(raw: Any) -> Any:
    """Make a tool's raw output navigable: parse JSON strings to dicts."""
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith(("{", "[")):
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                pass
    return raw


def resolve_path(dotted: str, bindings: dict[str, Any]) -> tuple[bool, Any]:
    """Resolve 'a.b.c' against bound outputs. Returns (found, value)."""
    parts = dotted.split(".")
    cur: Any = bindings
    for part in parts:
        cur = _coerce_output(cur) if isinstance(cur, str) else cur
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False, None
    return True, cur


def eval_condition(cond: dict[str, Any], bindings: dict[str, Any]) -> bool:
    """Evaluate {field, op, value} against bindings. Unknown field -> False."""
    if not isinstance(cond, dict):
        return False
    field = cond.get("field", "")
    if not isinstance(field, str) or not field:
        return False
    op = cond.get("op", "==")
    expected = cond.get("value")
    found, actual = resolve_path(field, bindings)
    if not found:
        return False
    try:
        if op == "==":
            return actual == expected
        if op == "!=":
            return actual != expected
        if op in (">", ">=", "<", "<="):
            a, e = float(actual), float(expected)
            return {">": a > e, ">=": a >= e, "<": a < e, "<=": a <= e}[op]
        if op == "in":
            return actual in expected
        if op == "not_in":
            return actual not in expected
        if op == "contains":
            return expected in actual
    except (TypeError, ValueError):
        return False
    return False


def render_template(text: str, bindings: dict[str, Any]) -> str:
    """Substitute {dotted.path} placeholders from bindings."""

    def _sub(match: re.Match) -> str:
        found, value = resolve_path(match.group(1), bindings)
        return str(value) if found else match.group(0)

    return _TEMPLATE_RE.sub(_sub, text)


def _render_args(args: Any, bindings: dict[str, Any]) -> Any:
    if isinstance(args, str):
        return render_template(args, bindings)
    if isinstance(args, dict):
        return {k: _render_args(v, bindings) for k, v in args.items()}
    if isinstance(args, list):
        return [_render_args(v, bindings) for v in args]
    return args


def validate_conditional_chain(
    chain: Any,
    allowed_tools: tuple[str, ...] | list[str] | set[str] | None = None,
) -> list[str]:
    """Return a list of spec errors; empty means valid.

    ``allowed_tools`` is optional. When it is None (the default) any
    tool name is accepted here and the caller's ``invoke`` callable is
    the authority: the conditional form is the old Aidon chain, which
    dispatches to the registered tool set, so a name that is not
    registered fails at invoke time and is recorded as an error.
    Pass a set to enforce a narrower allowlist for a specific caller.
    """
    errors: list[str] = []
    if not isinstance(chain, list) or not chain:
        return ["chain must be a non-empty list of steps"]
    for i, step in enumerate(chain):
        errors.extend(_validate_conditional_step(step, f"step {i}", allowed_tools))
    return errors


def _validate_conditional_step(
    step: Any, where: str,
    allowed_tools: tuple[str, ...] | list[str] | set[str] | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(step, dict):
        return [f"{where}: must be an object"]
    if "tool" in step:
        tool = step["tool"]
        if not isinstance(tool, str) or not tool.strip():
            errors.append(f"{where}: 'tool' must be a non-empty string")
        elif allowed_tools is not None and tool not in allowed_tools:
            errors.append(f"{where}: tool is not permitted: {tool}")
        if "args" in step and not isinstance(step["args"], (dict, list, str)):
            errors.append(f"{where}: 'args' must be an object, list, or string")
        if "as" in step and not isinstance(step["as"], str):
            errors.append(f"{where}: 'as' must be a string")
    elif "if" in step:
        cond = step["if"]
        if not isinstance(cond, dict):
            errors.append(f"{where}: 'if' must be {{field, op, value}}")
        else:
            if not isinstance(cond.get("field"), str) or not cond["field"].strip():
                errors.append(f"{where}: 'if.field' must be a non-empty string")
            if cond.get("op", "==") not in _CONDITION_OPS:
                errors.append(
                    f"{where}: 'if.op' must be one of {sorted(_CONDITION_OPS)}"
                )
            if "value" not in cond:
                errors.append(f"{where}: 'if' needs a 'value'")
        for branch in ("then", "else"):
            if branch in step:
                if not isinstance(step[branch], list):
                    errors.append(f"{where}: '{branch}' must be a list of steps")
                else:
                    for j, sub in enumerate(step[branch]):
                        errors.extend(
                            _validate_conditional_step(
                                sub, f"{where}.{branch}[{j}]", allowed_tools
                            )
                        )
    else:
        errors.append(f"{where}: must have 'tool' or 'if'")
    return errors


def run_conditional_chain(
    chain: list[dict[str, Any]],
    invoke: Callable[[str, dict[str, Any]], Any],
    allowed_tools: tuple[str, ...] | list[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Execute a conditional chain. Returns a summary of what ran.

    Defensive by design: validate first (shape, plus the caller's
    optional allowlist). Hand-edited job records cannot skip the shape
    check. Malformed steps after that are recorded in ``errors`` and
    skipped — one bad step never aborts the rest.
    """
    pre = validate_conditional_chain(chain, allowed_tools)
    if pre:
        return {
            "ok": False,
            "ran": [],
            "skipped": [],
            "errors": pre,
            "bindings": {},
        }
    bindings: dict[str, Any] = {}
    ran: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []

    def _run_steps(steps: Any, depth: int) -> None:
        if depth > _MAX_NESTING:
            errors.append(
                f"chain nesting exceeds {_MAX_NESTING}; deeper branch skipped"
            )
            return
        if not isinstance(steps, list):
            errors.append(
                f"chain branch is not a list ({type(steps).__name__}); skipped"
            )
            return
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                errors.append(f"step {idx}: not an object; skipped")
                continue
            if "tool" in step:
                name = step["tool"]
                if not isinstance(name, str) or not name.strip():
                    errors.append(
                        f"step {idx}: 'tool' must be a non-empty string; skipped"
                    )
                    continue
                if allowed_tools is not None and name not in allowed_tools:
                    errors.append(f"step {idx}: tool not permitted: {name}")
                    continue
                args = _render_args(step.get("args") or {}, bindings)
                if not isinstance(args, dict):
                    args = {}
                try:
                    result = invoke(name, args)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    log.warning("chain tool %r failed: %s", name, exc)
                    continue
                ran.append(name)
                as_name = step.get("as")
                if isinstance(as_name, str) and as_name:
                    bindings[as_name] = _coerce_output(result)
            elif "if" in step:
                cond = step["if"]
                if not isinstance(cond, dict):
                    errors.append(
                        f"step {idx}: 'if' must be {{field, op, value}}; skipped"
                    )
                    continue
                matched = eval_condition(cond, bindings)
                branch = (
                    step.get("then", []) if matched else step.get("else", [])
                )
                label = (
                    f"if {cond.get('field')} {cond.get('op')} {cond.get('value')}"
                )
                (ran if matched else skipped).append(label)
                _run_steps(branch, depth + 1)
            else:
                errors.append(
                    f"step {idx}: has neither 'tool' nor 'if'; skipped"
                )
                log.warning("chain %s", errors[-1])

    _run_steps(chain if isinstance(chain, list) else [], 0)
    return {
        "ok": not errors,
        "ran": ran,
        "skipped": skipped,
        "errors": errors,
        "bindings": bindings,
    }
