"""Autonomy metrics: a JSON event log under the Aidon home.

Every recorded event is one line of evidence about how much human
help a run needed: runs started, tasks finished, questions asked,
retries, skills created/promoted/retired/reused, tools adopted/reused,
router calls and tokens per tier, human interventions, quarantines,
and learn cycles. Nothing here calls a model and nothing is guessed:
the summary counts stored events inside a time window, and the render
prints exactly those counts.

Storage is a plain JSON file, <home>/autonomy_metrics.json, holding
{"events": [...]}. Each event carries its name, run id, detail text,
token count, a human_decided flag, and an ISO UTC timestamp. There is
no database and no migration: a missing or unreadable file reads as
an empty log, and the next record recreates it.

One rule matters for honesty: a human_intervention event counts in
the summary only when it was recorded with human_decided=True. An
intervention recorded without a human decision is still stored and
still visible to count_matching, but it is not evidence of human
help, so it does not raise the intervention count.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Canonical event names the summary groups by. record() accepts any
# string, so new event kinds can be logged without a code change, but
# only these names appear in summary() and render().
RUN_STARTED = "run_started"
TASK_SUCCESS = "task_success"
TASK_FAILED = "task_failed"
CLARIFYING_QUESTION = "clarifying_question"
RETRY = "retry"
SKILL_CREATED = "skill_created"
SKILL_PROMOTED = "skill_promoted"
SKILL_RETIRED = "skill_retired"
SKILL_REUSED = "skill_reused"
TOOL_ADOPTED = "tool_adopted"
TOOL_REUSED = "tool_reused"
ROUTER_FAST_CALL = "router_fast_call"
ROUTER_STRONG_CALL = "router_strong_call"
HUMAN_INTERVENTION = "human_intervention"
QUARANTINE = "quarantine"
LEARN_CYCLE = "learn_cycle"

_CANONICAL = (
    RUN_STARTED, TASK_SUCCESS, TASK_FAILED, CLARIFYING_QUESTION, RETRY,
    SKILL_CREATED, SKILL_PROMOTED, SKILL_RETIRED, SKILL_REUSED,
    TOOL_ADOPTED, TOOL_REUSED, ROUTER_FAST_CALL, ROUTER_STRONG_CALL,
    HUMAN_INTERVENTION, QUARANTINE, LEARN_CYCLE,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str):
    """Parse an ISO timestamp, accepting a trailing Z. None on failure."""
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AutonomyMetrics:
    """JSON-file autonomy event log for one Aidon home."""

    def __init__(self, home):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "autonomy_metrics.json"

    # -- storage -------------------------------------------------------

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        events = data.get("events") if isinstance(data, dict) else None
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)]

    def _save(self, events: list[dict]) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"events": events}, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    # -- recording -----------------------------------------------------

    def record(self, event: str, run_id: str = "", detail: str = "",
               tokens: int = 0, human_decided: bool = False) -> dict:
        """Append one event and return it. Empty event names are refused."""
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event name is required")
        entry = {
            "event": event,
            "run_id": str(run_id or ""),
            "detail": str(detail or ""),
            "tokens": int(tokens),
            "human_decided": bool(human_decided),
            "timestamp": _now_iso(),
        }
        events = self._load()
        events.append(entry)
        self._save(events)
        return entry

    def count_matching(self, event: str, run_id: str = "",
                       detail_contains: str = "") -> int:
        """Count stored events matching all given filters.

        No time window is applied here: this is a raw log query.
        The window lives in summary(), not here.
        """
        count = 0
        for entry in self._load():
            if entry.get("event") != event:
                continue
            if run_id and entry.get("run_id") != run_id:
                continue
            if detail_contains and detail_contains not in str(entry.get("detail", "")):
                continue
            count += 1
        return count

    # -- summary -------------------------------------------------------

    def _windowed(self, days: int) -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        kept = []
        for entry in self._load():
            stamped = _parse_iso(str(entry.get("timestamp", "")))
            if stamped is not None and stamped >= cutoff:
                kept.append(entry)
        return kept

    def summary(self, days: int = 30) -> dict:
        """Counts over the last `days` days, from stored events only."""
        events = self._windowed(days)

        def count(name: str) -> int:
            return sum(1 for entry in events if entry.get("event") == name)

        def tokens_for(name: str) -> int:
            total = 0
            for entry in events:
                if entry.get("event") == name:
                    try:
                        total += int(entry.get("tokens") or 0)
                    except (TypeError, ValueError):
                        continue
            return total

        succeeded = count(TASK_SUCCESS)
        failed = count(TASK_FAILED)
        finished = succeeded + failed
        reused = [entry for entry in events if entry.get("event") == SKILL_REUSED]
        reuse_success = sum(
            1 for entry in reused if "success" in str(entry.get("detail", "")).lower())
        reuse_failure = sum(
            1 for entry in reused if "failure" in str(entry.get("detail", "")).lower())
        return {
            "window_days": days,
            "runs_started": count(RUN_STARTED),
            "tasks_succeeded": succeeded,
            "tasks_failed": failed,
            "success_rate": (succeeded / finished) if finished else 0.0,
            "clarifying_questions": count(CLARIFYING_QUESTION),
            "retries": count(RETRY),
            "skills_created": count(SKILL_CREATED),
            "skills_promoted": count(SKILL_PROMOTED),
            "skills_retired": count(SKILL_RETIRED),
            "skill_reuses": len(reused),
            "tools_adopted": count(TOOL_ADOPTED),
            "tool_reuses": count(TOOL_REUSED),
            "router_fast_calls": count(ROUTER_FAST_CALL),
            "router_fast_tokens": tokens_for(ROUTER_FAST_CALL),
            "router_strong_calls": count(ROUTER_STRONG_CALL),
            "router_strong_tokens": tokens_for(ROUTER_STRONG_CALL),
            # Only human-decided interventions count: an intervention
            # recorded without human_decided=True is stored, not counted.
            "human_interventions": sum(
                1 for entry in events
                if entry.get("event") == HUMAN_INTERVENTION
                and entry.get("human_decided") is True),
            "quarantines": count(QUARANTINE),
            "learn_cycles": count(LEARN_CYCLE),
            "skill_reuse_success_rate": (reuse_success / len(reused)) if reused else 0.0,
            "skill_failure_rate": (reuse_failure / len(reused)) if reused else 0.0,
        }

    def render(self, days: int = 30) -> str:
        """Markdown summary: one line per metric, numbers from summary()."""
        s = self.summary(days)
        lines = [
            f"# Autonomy report — last {s['window_days']} days",
            "",
            f"- runs started: {s['runs_started']}",
            f"- tasks succeeded: {s['tasks_succeeded']}",
            f"- tasks failed: {s['tasks_failed']}",
            f"- success rate: {s['success_rate']:.1%}",
            f"- clarifying questions: {s['clarifying_questions']}",
            f"- retries: {s['retries']}",
            f"- skills created: {s['skills_created']}",
            f"- skills promoted: {s['skills_promoted']}",
            f"- skills retired: {s['skills_retired']}",
            f"- skill reuses: {s['skill_reuses']}",
            f"- skill reuse success rate: {s['skill_reuse_success_rate']:.1%}",
            f"- skill failure rate: {s['skill_failure_rate']:.1%}",
            f"- tools adopted: {s['tools_adopted']}",
            f"- tool reuses: {s['tool_reuses']}",
            f"- router fast calls: {s['router_fast_calls']}",
            f"- router fast tokens: {s['router_fast_tokens']}",
            f"- router strong calls: {s['router_strong_calls']}",
            f"- router strong tokens: {s['router_strong_tokens']}",
            f"- human interventions: {s['human_interventions']}",
            f"- quarantines: {s['quarantines']}",
            f"- learn cycles: {s['learn_cycles']}",
        ]
        return "\n".join(lines) + "\n"
