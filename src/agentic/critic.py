"""Plan critic: reflection before execution, to reduce revision.

Deterministic validation (validator.py) rejects plans with
unknown tools, bad paths, or duplicate deliverables. It cannot
judge coverage: a plan can be valid yet miss a step the goal
needs, and that gap only surfaces at verification, after
execution — a full replan. The critic closes that gap with
one model call on the finished plan, before any step runs:

- coverage: every part of the goal has a step,
- ordering: steps run in an order that can work,
- grounding: no step invents a tool or skill outside the
  listed sets (the deterministic validator already rejects
  unknown tools; the critic catches invented SKILL names
  and tools the plan merely implies).

The critic advises, never blocks: issues feed the same
bounded repair path as validation failures, up to
AIDON_CRITIC_MAX_ROUNDS rounds, and when rounds run out
the latest plan proceeds with the issues traced. A critic
that could veto a plan forever would be worse than no
critic. Without a model client the critic returns no
issues — the deterministic validator is still the floor.
Each round returns at most AIDON_CRITIC_MAX_ISSUES issues
(default 5): the cap bounds how much repair one round can
ask for, so a long complaint list cannot stall the run.
"""

from __future__ import annotations

import json
import os
import re

from ..system.model_client import Message

_DEFAULT_MAX_ISSUES = 5


def max_issues() -> int:
    """Critic issues per round, from AIDON_CRITIC_MAX_ISSUES.

    Read at call time, never import time. Default 5; garbage
    falls back to 5; below 1 clamps to 1 (a critic returning
    nothing still costs a call, so 0 is not a useful setting);
    above 20 clamps to 20, the settings page's bound.
    """
    try:
        value = int(str(os.environ.get("AIDON_CRITIC_MAX_ISSUES", "")).strip())
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ISSUES
    if value < 1:
        return 1
    if value > 20:
        return 20
    return value

_CRITIC_SYSTEM = (
    "Review this plan against its goal. Return JSON only, "
    "exactly {\"issues\": [\"...\", ...]}. An issue is a concrete "
    "gap: a part of the goal with no step, a step order that "
    "cannot work, or a tool or skill name that is not in the "
    "listed sets. Do not invent issues about style. An empty "
    "list means the plan covers the goal."
)


def _extract_json(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            return None
    return None


def review_plan(plan, goal: str, available_tools=None,
                skill_names=None, chat=None) -> list[str]:
    """Return critic issues for a plan, empty when clean.

    plan may be a Plan object or a dict. chat, when given, is
    a callable taking a message list and returning reply text;
    without it only the (empty) model layer is skipped — the
    deterministic validator remains the caller's floor.
    """
    if chat is None:
        return []
    tools = sorted({str(t) for t in (available_tools or [])})
    skills = sorted({str(s) for s in (skill_names or [])})
    if hasattr(plan, "normalized_steps"):
        steps = [{"tool": str(s.tool), "args": s.args,
                  "deliverable": str(s.deliverable)}
                 for s in plan.normalized_steps()]
        deliverables = list(getattr(plan, "deliverables", []) or [])
    elif isinstance(plan, dict):
        steps = plan.get("steps", []) or []
        deliverables = plan.get("deliverables", []) or []
    else:
        steps, deliverables = [], []
    prompt = (
        f"Goal: {goal}\n"
        f"Plan steps: {json.dumps(steps)[:3000]}\n"
        f"Plan deliverables: {json.dumps(deliverables)[:1000]}\n"
        f"Available tools: {', '.join(tools)}\n"
        f"Available skills: {', '.join(skills) if skills else '(none)'}\n"
    )
    try:
        reply = chat([Message(role="system", content=_CRITIC_SYSTEM),
                      Message(role="user", content=prompt)])
    except Exception:
        return []
    data = _extract_json(str(reply))
    if not isinstance(data, dict):
        return []
    issues = data.get("issues", [])
    if not isinstance(issues, list):
        return []
    return [str(issue).strip() for issue in issues
            if str(issue).strip()][:max_issues()]
