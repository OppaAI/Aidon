"""Multi-agent research: several researcher angles, merged, never one failure kills the run.

One researcher, one query, tends to return one angle. Splitting
the same query across angles - overview, details, counterpoints
by default - and running them in parallel gives a wider fact
set, and a counterpoint angle is what stops a one-sided answer
from looking like a consensus.

How it works:
- run_research_agents(query, researcher, angles=None) runs
  researcher(query, angle) for each angle, in parallel, with a
  ThreadPoolExecutor capped at 3 workers, so a slow angle does
  not block the others and the run does not fan out without
  bound.
- Facts are merged deduplicated, preserving first-seen order,
  so the same fact from two angles appears once, and the
  order is deterministic: angles in the order given, facts
  in the order each researcher returned them.
- A researcher failure for one angle does not fail the whole
  run. That angle is recorded in failed_angles, the other
  angles' facts are still returned, and the caller can see
  exactly which angle is missing instead of getting an empty
  result or an exception that hides the partial work.

The researcher is injected, a callable taking (query, angle)
and returning a list of fact strings. No network, no model
call, and no search backend lives in this module, so tests
can pass a fake researcher and production can pass a real
one without changing this code.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

DEFAULT_ANGLES = ["overview", "details", "counterpoints"]


def run_research_agents(query: str, researcher, angles=None) -> dict:
    """Run one researcher per angle in parallel, merge facts, report failures."""
    if not query or not query.strip():
        raise ValueError("research query is required")
    if researcher is None:
        raise ValueError("researcher is required")
    angle_list = list(angles) if angles else list(DEFAULT_ANGLES)
    facts: list[str] = []
    seen: set[str] = set()
    failed_angles: list[str] = []

    def _run(angle: str):
        return researcher(query, angle)

    with ThreadPoolExecutor(max_workers=3) as pool:
        # Submit in angle order; results are collected in angle
        # order below, so merging is deterministic regardless of
        # which thread finishes first.
        futures = {angle: pool.submit(_run, angle) for angle in angle_list}
        for angle in angle_list:
            try:
                result = futures[angle].result()
            except Exception:
                failed_angles.append(angle)
                continue
            if result is None:
                continue
            if not isinstance(result, list):
                failed_angles.append(angle)
                continue
            for fact in result:
                if not isinstance(fact, str):
                    continue
                fact = fact.strip()
                if not fact or fact in seen:
                    continue
                seen.add(fact)
                facts.append(fact)

    return {
        "query": query,
        "angles": angle_list,
        "facts": facts,
        "failed_angles": failed_angles,
    }
