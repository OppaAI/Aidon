"""Clef validate gate: a cheap triage decision between code
verification and the model verifier.

The pipeline today is code verify (deterministic, always)
then, only when enabled, the model verifier (a full LLM
call). The gate sits between them and answers one choice
question: accept, retry, or escalate. It never overrides
code verification — deterministic checks are ground truth,
probabilities are not proof — and it never writes a repair
plan or judges content on its own.

Decision table, mirroring triage and DAG ranking:
- accept, confidence >= 0.6: skip the model verifier, but
  ONLY for structured deliverables (JSON, JSONL, CSV, TSV).
  A short text sample is not evidence that a report,
  recipe, or document is correct, and the gate must not
  override an explicit "always" for content. For prose,
  document, code, or image deliverables an accept verdict
  defers to the model verifier exactly like escalate: the
  gate may recommend, never declare content correct. In
  selective mode this is consistent by construction, since
  selective already skips the verifier for structured-only
  runs and runs it for everything else; in always mode it
  restores the mode's meaning. When the verifier is off,
  the gate changes nothing either way.
- retry, confidence >= 0.8: treat as a generic model NO.
  The reason names no file, so the deadlock rule keeps
  every deliverable repairable and the repair loop re-runs
  their steps. This is the gate acting as a cheap verifier
  even when the LLM verifier is off.
- anything else (escalate, or confidence below the bar):
  defer to the model verifier as today. If the verifier is
  off, the run proceeds as verified: an uncertain gate
  never blocks a code-passing run.

State size is capped on purpose: goal, deliverable names
with sizes and short text samples, and criteria, truncated
to a few thousand characters total. How much file text fits
before decision quality drops is still unmeasured; the cap
keeps the gate cheap (a few hundred input tokens, zero
output) while that measurement is pending. Binary and
missing deliverables are described, never dumped.
"""

from __future__ import annotations

from pathlib import Path

ACCEPT_CONFIDENCE = 0.6
RETRY_CONFIDENCE = 0.8
MAX_STATE_CHARS = 2000
MAX_SAMPLE_CHARS = 400
MAX_CRITERIA_CHARS = 500
# The selective verifier's skip set, reused here so the two
# policies cannot drift: code checks are complete for these,
# and a model opinion adds no quality signal.
STRUCTURED_SUFFIXES = {".json", ".jsonl", ".csv", ".tsv"}

ACTION_CRITERIA = {
    "accept": "The deliverables satisfy the goal and the criteria; "
              "finish, no further verification needed.",
    "retry": "The deliverables are flawed but repairable; repair "
             "them and re-verify.",
    "escalate": "Cannot judge from this summary, or the issue needs "
                "the full model verifier or the user.",
}


def summarize_deliverables(workdir, deliverables) -> str:
    """One short line per deliverable: name, size, text sample
    for readable files, a marker for binary or missing ones."""
    workdir = Path(workdir)
    lines = []
    budget = MAX_STATE_CHARS
    for name in deliverables:
        if budget <= 0:
            break
        path = workdir / str(name)
        try:
            if not path.is_file():
                lines.append(f"- {name}: [missing]")
                continue
            size = path.stat().st_size
            try:
                from .verify import read_deliverable
                info = read_deliverable(path)
            except Exception:
                info = None
            if info is not None and info.get("kind") in ("text", "extracted"):
                sample = str(info.get("shown", ""))[:MAX_SAMPLE_CHARS]
                lines.append(f"- {name} ({size} bytes): {sample}")
            else:
                lines.append(f"- {name} ({size} bytes, binary or opaque)")
        except Exception:
            lines.append(f"- {name}: [unreadable]")
        budget = MAX_STATE_CHARS - sum(len(line) for line in lines)
    return "\n".join(lines)


def accept_eligible(deliverables) -> bool:
    """True only when every deliverable is structured data.
    A content deliverable (prose, document, code, image, or
    a bare name with no suffix) never earns a verifier skip
    from the gate, whatever Clef's confidence."""
    if not deliverables:
        return False
    for name in deliverables:
        if Path(str(name)).suffix.lower() not in STRUCTURED_SUFFIXES:
            return False
    return True


def validate_gate(goal: str, deliverables, criteria, workdir, client) -> dict:
    """One Clef choice -> {action, confidence, reason}.

    client None means the gate is skipped: escalate, so the
    caller runs the model verifier as today. A transport or
    parse failure raises, and the caller treats it the same
    way (defer, never block).
    """
    if client is None:
        return {"action": "escalate", "confidence": 0.0, "reason": ""}
    summary = summarize_deliverables(workdir, deliverables)
    criteria_text = "\n".join(str(c) for c in (criteria or []))
    state = {"goal": goal, "deliverables": summary,
             "criteria": criteria_text[:MAX_CRITERIA_CHARS]}
    choice, confidence, _probs = client.decide_choice(
        state, "next_action",
        "Given the goal, the deliverable summaries, and the "
        "criteria, what should verification do next?",
        ACTION_CRITERIA)
    if choice == "accept" and confidence >= ACCEPT_CONFIDENCE:
        if not accept_eligible(deliverables):
            # A sample that looks good is not evidence about
            # content; defer to the model verifier instead.
            return {"action": "escalate", "confidence": confidence,
                    "reason": ""}
        return {"action": "accept", "confidence": confidence,
                "reason": "accepted deliverables"}
    if choice == "retry" and confidence >= RETRY_CONFIDENCE:
        return {"action": "retry", "confidence": confidence,
                "reason": "deliverables need repair"}
    return {"action": "escalate", "confidence": confidence, "reason": ""}
