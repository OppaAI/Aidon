"""Scheduler loop, daily report, approval nudges, autonomy.

Old keys, only: AIDON_DAILY_REPORT_TIME,
AIDON_APPROVAL_NUDGE_MIN, AIDON_APPROVAL_NUDGE_CHANNEL,
AIDON_AUTONOMY_ENABLED, AIDON_AUTONOMY_THRESHOLD,
AIDON_AUTONOMY_DECAY_DAYS, AIDON_OWNER_EMAIL,
AIDON_CORRECTED_SUBJECT. No new duplicate.
SchedulerLoop polls Scheduler.check_due, it does not
invent a new scheduler, and it stops on a threading
Event, so tests run it for a fraction of a second.
Daily report reads trace files and learned/last_run.json
in the Aidon home, it never invents runs, an empty home
is an empty report, not a fabricated one.
Approval nudges: a pending approval older than
AIDON_APPROVAL_NUDGE_MIN minutes, to
AIDON_APPROVAL_NUDGE_CHANNEL, via a Slack sender, once
per approval, never a loop.
Autonomy: low and medium stakes classes earn standing
permission after AIDON_AUTONOMY_THRESHOLD consecutive
explicit approvals, high stakes never graduates, and
the count decays after AIDON_AUTONOMY_DECAY_DAYS.
This is the old graduated approval, not a blanket
auto-approval, and it is off when AIDON_AUTONOMY_ENABLED
is 0.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class SchedulerLoop:
    def __init__(self, scheduler, dispatcher, interval_seconds: float = 60.0):
        self.scheduler = scheduler
        self.dispatcher = dispatcher
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_once(self) -> list:
        due = self.scheduler.check_due()
        for event in due:
            self.dispatcher.dispatch(event)
        return due

    def run(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(timeout=self.interval_seconds)


def daily_report(home: Path) -> str:
    home = Path(home)
    lines = [f"# Daily report {datetime.now(timezone.utc).date().isoformat()}", ""]
    last_run = home / "learned" / "last_run.json"
    if last_run.exists():
        try:
            data = json.loads(last_run.read_text())
            lines.append(f"Last run: {data.get('status', '')} project {data.get('project_id', '')}")
        except Exception:
            lines.append("Last run: unreadable")
    else:
        lines.append("Last run: none")
    trace_dir = home / "trace"
    count = len(list(trace_dir.glob("*.jsonl"))) if trace_dir.exists() else 0
    lines.append(f"Trace files: {count}")
    return "\n".join(lines) + "\n"


class NudgeTracker:
    def __init__(self, nudge_minutes: int = 15, channel: str = ""):
        self.nudge_minutes = nudge_minutes
        self.channel = channel
        self._nudged: set[str] = set()

    def check(self, approval_id: str, pending_since: float, now: float,
              slack_sender=None) -> bool:
        if approval_id in self._nudged:
            return False
        if self.nudge_minutes <= 0:
            return False
        if (now - pending_since) < self.nudge_minutes * 60:
            return False
        self._nudged.add(approval_id)
        if slack_sender is not None and self.channel:
            slack_sender(self.channel, f"Approval pending: {approval_id}")
        return True


class AutonomyTracker:
    def __init__(self, enabled: bool = True, threshold: int = 5,
                 decay_days: float = 14.0):
        self.enabled = enabled
        self.threshold = threshold
        self.decay_days = decay_days
        self._counts: dict[str, int] = {}
        self._last: dict[str, float] = {}

    def record_approval(self, stake: str, now: float):
        self._counts[stake] = self._counts.get(stake, 0) + 1
        self._last[stake] = now

    def record_rejection(self, stake: str):
        self._counts[stake] = 0

    def should_auto_approve(self, stake: str, now: float) -> bool:
        if not self.enabled or stake == "high":
            return False
        last = self._last.get(stake, 0)
        if last and (now - last) > self.decay_days * 86400:
            self._counts[stake] = 0
            return False
        return self._counts.get(stake, 0) >= self.threshold
