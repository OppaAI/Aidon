"""Scheduler loop, daily report, approval nudges, autonomy.

Old keys, only: AIDON_DAILY_REPORT_TIME,
AIDON_APPROVAL_NUDGE_MIN, AIDON_APPROVAL_NUDGE_CHANNEL,
AIDON_AUTONOMY_ENABLED, AIDON_AUTONOMY_THRESHOLD,
AIDON_AUTONOMY_DECAY_DAYS, AIDON_OWNER_EMAIL,
AIDON_CORRECTED_SUBJECT. No new duplicate.
SchedulerLoop polls durable scheduler claims, it does not
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

import threading
from datetime import datetime
from pathlib import Path


class SchedulerLoop:
    """Poll due jobs, notify, dispatch, then drain the outbox.

    on_due is the scheduled-run notify hook: it is called with each
    due event BEFORE dispatch, so a scheduled run is announced
    even if dispatch then fails. A scheduled run that needs
    approval still goes through the normal approval gate, which
    fails closed on timeout — there is no separate approval path
    for scheduled runs, and on_due never approves anything.
    Outbox draining is cadence-spaced (see Outbox): run_once
    drains only due notifications through outbox_sender, and
    records the result on last_outbox_drain instead of
    returning it, so run_once still returns the due events.
    """

    def __init__(self, scheduler, dispatcher=None, interval_seconds: float = 60.0,
                 outbox=None, outbox_sender=None, on_due=None):
        self.scheduler = scheduler
        self.dispatcher = dispatcher
        self.interval_seconds = interval_seconds
        self.outbox = outbox
        self.outbox_sender = outbox_sender
        self.on_due = on_due
        self.last_outbox_drain = None
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def drain_outbox(self) -> dict | None:
        """Drain the outbox once through outbox_sender, if both are set."""
        if self.outbox is None or self.outbox_sender is None:
            return None
        self.last_outbox_drain = self.outbox.drain(self.outbox_sender)
        return self.last_outbox_drain

    def run_once(self, now: datetime | None = None) -> list:
        now = now or datetime.now()
        claim_due = getattr(self.scheduler, "claim_due", None)
        due = (claim_due(now) if callable(claim_due)
               else self.scheduler.check_due(now))
        for event in due:
            if self.on_due is not None:
                self.on_due(event)
            if self.dispatcher is not None:
                self.dispatcher.dispatch(event)
                complete = getattr(self.scheduler, "complete", None)
                if callable(complete):
                    complete(event)
        self.drain_outbox()
        return due

    def run(self):
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(timeout=self.interval_seconds)


def daily_report(home: Path) -> str:
    """Return the canonical daily report used by CLI and WebUI."""
    from .report import daily_report as _daily_report
    return _daily_report(home)


class NudgeTracker:
    def __init__(self, nudge_minutes: int = 15, channel: str = ""):
        self.nudge_minutes = nudge_minutes
        self.channel = channel
        self._nudged: set[str] = set()

    def check(self, approval_id: str, pending_since: float, now: float,
              slack_sender=None, note: str = "",
              min_elapsed_seconds: float | None = None) -> bool:
        if approval_id in self._nudged:
            return False
        if self.nudge_minutes <= 0:
            return False
        threshold = (min_elapsed_seconds if min_elapsed_seconds is not None
                     else self.nudge_minutes * 60)
        if (now - pending_since) < threshold:
            return False
        self._nudged.add(approval_id)
        if slack_sender is not None and self.channel:
            text = f"Approval pending: {approval_id}"
            if note:
                text += f" — {note}"
            slack_sender(self.channel, text)
        return True

