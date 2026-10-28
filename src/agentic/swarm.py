"""Multi-agent swarm, Aidon decides when to use it, not a flag.

Swarm is orchestration, not a model feature: several
workers, each calling the same Spark model, in parallel,
with a shared budget, then a merge. Aidon decides, by
asking the model to split the goal: if the model returns
SINGLE, or an empty reply, or fewer than two subgoals,
there is no swarm, the goal runs as one. If it returns
two or more independent subgoals, one per line, those
run in parallel, in order, with a ThreadPoolExecutor,
and the results are merged in subgoal order, never
reordered by completion time.
Rate limits and credits are the constraint, not the
model: a swarm multiplies calls, so max_workers is
capped at 3 by default, and a budget, when given, is
shared, not per worker. Swarm use is off by
default, on only when the goal enumerated the split;
here Aidon decides every time, by the split call.
A runner is a callable goal -> text, in tests a fake,
in production a GoalRunner run, a research run, or a
plan step run. A worker failure is a result, not a
crash, and the merge marks it failed.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..system.model_client import Message, ModelClient


def decide_subgoals(goal: str, client: ModelClient) -> list[str]:
    prompt = (f"Split this goal into independent subgoals, one per line, "
              f"only if it genuinely splits into two or more independent "
              f"parts that can run in parallel. Otherwise reply SINGLE.\n\nGoal: {goal}")
    reply = client.chat([Message("system", "You split goals, or reply SINGLE."),
                         Message("user", prompt)]).strip()
    if not reply or reply.upper().startswith("SINGLE"):
        return []
    lines = []
    for line in reply.splitlines():
        line = line.strip().lstrip("0123456789.-* ").strip()
        if line and line.upper() != "SINGLE":
            lines.append(line)
    return lines if len(lines) >= 2 else []


def run_swarm(goal: str, client: ModelClient, runner, max_workers: int = 3) -> dict:
    subgoals = decide_subgoals(goal, client)
    if not subgoals:
        # No swarm: the caller runs the goal once itself, so
        # run_swarm must not run it here too — the old code
        # executed runner(goal) and the caller then ran it
        # again and discarded this result (double model calls,
        # double sandbox writes, approvals asked twice).
        return {"swarmed": False, "subgoals": [], "results": [],
                "merged": None}
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(subgoals)))) as pool:
        futures = [pool.submit(_run_one, runner, subgoal) for subgoal in subgoals]
        results = [f.result() for f in futures]
    merged = "\n\n".join(f"## {subgoal}\n{result}" for subgoal, result in zip(subgoals, results))
    return {"swarmed": True, "subgoals": subgoals, "results": results, "merged": merged}


def _run_one(runner, subgoal: str) -> str:
    try:
        return runner(subgoal)
    except Exception as exc:
        return f"failed: {exc}"
