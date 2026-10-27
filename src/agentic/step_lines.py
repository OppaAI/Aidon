"""Step lines and timestamped attempts.

A step line is one attempt at one step: what tool ran, whether
it succeeded, when it started and ended, and a short detail.
Rendering them in order is the run's audit trail, and elapsed
time comes from the timestamps, never from a model's estimate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


@dataclass
class StepLine:
    step_id: str
    tool: str
    status: str
    detail: str = ""
    started_at: str = field(default_factory=_now)
    ended_at: str = field(default_factory=_now)

    def elapsed_seconds(self) -> float:
        start, end = _parse(self.started_at), _parse(self.ended_at)
        if start is None or end is None:
            return 0.0
        return max(0.0, (end - start).total_seconds())


def record_attempt(lines: list, step_id: str, tool: str, status: str,
                   detail: str = "", started_at: str | None = None,
                   ended_at: str | None = None) -> StepLine:
    """Append one attempt line with UTC timestamps, returning it."""
    line = StepLine(step_id=step_id, tool=tool, status=status, detail=detail,
                    started_at=started_at or _now(),
                    ended_at=ended_at or _now())
    lines.append(line)
    return line


def render_step_lines(lines: list) -> str:
    """One text line per attempt: id, tool, status, elapsed, detail."""
    rendered = []
    for line in lines:
        text = (f"{line.step_id} {line.tool} {line.status} "
                f"{line.elapsed_seconds():.1f}s")
        if line.detail:
            text += f" {line.detail}"
        rendered.append(text)
    return "\n".join(rendered)
