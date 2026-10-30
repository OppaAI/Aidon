"""Clef recovery router: classify a failed attempt before
replanning, so the loop stops reasoning from scratch after
every failure.

Today every failure triggers a repaired plan, even when
the failure is transient (a timeout, a flaky API) and the
plan is fine, or when the missing piece is a capability
rather than a bad artifact. The router asks Clef one
choice question over the goal, the attempt number, and
the failure texts, and the loop maps the answer onto
paths that already exist:

- retry_execution: re-run the same plan unchanged next
  attempt. No repaired plan is asked for, so a transient
  failure costs one attempt instead of a replan that may
  rewrite a good plan. Once per run only: a misclassified
  persistent failure must not burn every attempt on
  identical re-execution.
- repair_artifact: the current path. Ask for a repaired
  plan with the failure context.
- acquire_capability: a tool, package, skill, or MCP
  connection is suspected missing. The repair context
  gains a note steering the planner toward the
  capability ladder (find, build, or connect) instead of
  rewriting the artifact. The planner still does the
  actual finding; Clef only names the direction.
- escalate: the failure is ambiguous or needs a user
  decision, permission, or credentials. At high
  confidence the run stops instead of burning attempts;
  below the bar it falls back to repair_artifact.

Confidence bars mirror the gate and DAG ranking: a route
acts at 0.6 or above, escalate stops the run only at 0.8
or above, and anything below the bar, any error, or no
client is the current repair path. The router never
overrides a failed deterministic check and never invents
a fix; it only selects which existing path runs next.

Criterion failures never reach the router: the
criterion-repair block and its fail-fast run first, so
this module only sees artifact, step, and model-verifier
failures.
"""

from __future__ import annotations

ROUTE_CONFIDENCE = 0.6
ESCALATE_CONFIDENCE = 0.8
MAX_FAILURE_CHARS = 1500

ROUTE_CRITERIA = {
    "retry_execution": "The failure looks transient: a timeout, "
                       "rate limit, network or API error, or an "
                       "interrupted run. Re-running the same steps "
                       "may succeed without changing the plan.",
    "repair_artifact": "The output is wrong, incomplete, or broken. "
                       "The plan or the artifact needs to change.",
    "acquire_capability": "A needed tool, package, skill, or MCP "
                          "connection is missing or unknown. The fix "
                          "is finding, building, or connecting it, "
                          "not rewriting the artifact.",
    "escalate": "The failure is ambiguous, needs a user decision, "
                "permission, or credentials, or cannot be safely "
                "routed to repair.",
}


def route_failure(goal: str, attempt: int, failures, client) -> dict:
    """One Clef choice -> {route, confidence, reason}.

    client None means no routing: repair_artifact, the
    current behavior. A transport or parse failure raises,
    and the caller treats it the same way (repair as today).
    An unknown choice falls back to repair_artifact rather
    than acting on a route the loop does not know.
    """
    if client is None:
        return {"route": "repair_artifact", "confidence": 0.0, "reason": ""}
    failure_text = "\n".join(str(f) for f in (failures or []))
    state = {"goal": goal, "attempt": attempt,
             "failures": failure_text[:MAX_FAILURE_CHARS]}
    route, confidence, _probs = client.decide_choice(
        state, "recovery_route",
        "Given the goal, the attempt number, and the failure "
        "texts, what should the run do next?",
        ROUTE_CRITERIA)
    if route not in ROUTE_CRITERIA:
        return {"route": "repair_artifact", "confidence": 0.0, "reason": ""}
    return {"route": route, "confidence": confidence, "reason": ""}
