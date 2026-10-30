"""Clef DAG matching: re-rank recalled workflows by meaning, not
word shape.

Recall ranks experiences by goal-shape overlap, which is
Jaccard over key tokens. Two goals that share words but mean
different things rank high; two goals that mean the same
thing in different words rank low. That mis-ranking is what
adaptation inherits: the loop adapts experiences[0], whatever
it is.

rank_dags() takes the already-recalled list (cheap Jaccard
pre-filter, unchanged) and asks Clef one choice question:
which candidate workflow fits this goal best, or none? The
candidate descriptions are goal + deliverable names + tools,
kept short so the state stays small. One call, zero output
tokens, a winner plus confidence.

Confidence is the safety valve, mirroring triage:
- below 0.6: keep the Jaccard order (uncertain ranking never
  overrides recall).
- a winner at 0.6 or above: move it to the front. The rest
  keep their recalled order.
- "none" at 0.8 or above: return an empty list, so the run
  plans fresh instead of adapting a workflow that fits
  nothing. Below 0.8, "none" keeps the Jaccard order: a
  doubtful veto must not throw away a usable workflow.
- any error, or fewer than two candidates: return the input
  unchanged. With one candidate there is nothing to rank;
  the veto for a single candidate is a follow-up, not this
  step, and a bad single adaptation is still caught by
  verification downstream.

The Jaccard threshold (AIDON_DAG_SIMILARITY) still decides
which candidates reach this step at all; Clef only reorders
within the recalled set. It never invents a workflow and
never changes what adaptation does with the winner.
"""

from __future__ import annotations

MIN_CONFIDENCE = 0.6
VETO_CONFIDENCE = 0.8
MAX_CANDIDATES = 5


def _candidate_text(exp) -> str:
    deliverables = []
    for node in (getattr(exp, "dag_nodes", None) or []):
        if isinstance(node, dict) and node.get("deliverable"):
            deliverables.append(str(node["deliverable"]))
    tools = getattr(exp, "tools_used", None) or []
    parts = [f"Goal: {getattr(exp, 'goal', '')}"]
    if deliverables:
        parts.append("Deliverables: " + ", ".join(deliverables[:8]))
    if tools:
        parts.append("Tools: " + ", ".join(str(t) for t in tools[:8]))
    return " | ".join(parts)


def rank_dags(goal: str, experiences: list, client, limit: int = MAX_CANDIDATES) -> list:
    """Reorder recalled experiences by Clef fit; input unchanged
    on any failure, low confidence, or fewer than two candidates."""
    if client is None or len(experiences) < 2:
        return experiences
    candidates = list(experiences[:limit])
    criteria = {f"dag_{i + 1}": _candidate_text(exp)
                for i, exp in enumerate(candidates)}
    criteria["none"] = ("No candidate workflow fits this goal; "
                        "plan fresh instead of adapting.")
    try:
        choice, confidence, _probs = client.decide_choice(
            {"goal": goal},
            "best_workflow",
            "Which candidate workflow fits this goal best? Judge by "
            "what the workflow actually does (its goal, deliverables, "
            "and tools), not by shared words with the goal.",
            criteria)
    except Exception:
        return experiences
    if confidence < MIN_CONFIDENCE:
        return experiences
    if choice == "none":
        if confidence >= VETO_CONFIDENCE:
            return []
        return experiences
    try:
        idx = int(choice.split("_", 1)[1]) - 1
    except (ValueError, IndexError):
        return experiences
    if not 0 <= idx < len(candidates):
        return experiences
    winner = candidates[idx]
    rest = [exp for exp in experiences if exp is not winner]
    return [winner] + rest
