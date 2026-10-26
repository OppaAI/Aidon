"""Triggers: what starts a DAG run.

Answer, fixed:
Yes, scheduler, message, WebUI, and the ingest folder
are all triggers, and they are all the same shape.
A trigger does not run a DAG by itself, and it does
not approve anything. A trigger produces a TriggerEvent,
the dispatcher hands that event to a run handler, the
handler builds a spec and a plan, and the approval gate
still applies before any live action. A trigger starts
work, it never bypasses approval, spec, or verification.

Trigger sources, allowed, and nothing else:
- scheduler: a time arrives, for example 3:00pm on the
  first Wednesday of the month, for the accounting emails.
- whatsapp: an incoming WhatsApp message, two-way.
- slack: an incoming Slack message, two-way.
- webui: a message, a button, or an upload in the WebUI.
- ingest: a new or changed file in the ingest folder.
- webhook: an HTTP POST, the n8n-style webhook trigger.
- manual: a user clicks Run, or runs the CLI.
Telegram is NOT a source, by owner decision. A Telegram
event is rejected, not routed, not silently dropped
into another source.

Ingest folder, user-specific path:
The ingest folder is <home>/ingest by default, or
AIDON_INGEST_DIR if set. It is the user's own path,
never a shared path, never a hardcoded system path.
A file dropped there is a trigger: the watcher scans,
reports new or changed files only, records what it
has seen, and does not move, delete, or overwrite the
user's file. What happens to the file next is a DAG
step, for example ingest into KB or library, not the
watcher itself.

Scheduler, minimal and testable:
A job is a name, a schedule string, a goal, and a
project id. Schedule strings supported:
- "daily HH:MM"
- "weekly Mon HH:MM" (Mon, Tue, Wed, Thu, Fri, Sat, Sun)
- "monthly first Wed HH:MM" (first weekday of the month)
- "monthly HH:MM day D" (day of month, 1 to 28)
- "cron M H DOM MON DOW" (5 fields, * or a single number)
next_due(after) returns the next occurrence after a
time, in local time, no timezone conversion in the
scheduler itself. check_due(now) returns jobs whose
next occurrence after their last run is at or before
now, and updates last run, so a job does not fire
twice for the same occurrence.

n8n triggers, mapping to Aidon:
- Schedule -> scheduler, above.
- Webhook -> webhook trigger, below, hosted by the
  WebUI server when it exists, callable from CLI now
  for testing.
- Manual -> manual trigger, WebUI button or CLI run.
- File watcher -> ingest folder trigger, above.
- Message, WhatsApp or Slack -> msg trigger, below,
  two-way: the event carries a reply callback, the
  handler's reply goes back on the same transport.
- Email, RSS, database, and other n8n triggers ->
  not core triggers. They are plugins later, if a
  task needs them, not built into the trigger core.
  Email in particular is not added here, it would
  need a mail plugin and its own approval rules.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path

ALLOWED_SOURCES = ("scheduler", "whatsapp", "slack", "webui",
                   "ingest", "webhook", "manual")
WEEKDAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3,
            "Fri": 4, "Sat": 5, "Sun": 6}


@dataclass
class TriggerEvent:
    source: str
    kind: str = ""
    goal: str = ""
    project_id: str = "default"
    payload: dict = field(default_factory=dict)
    received_at: str = ""
    reply: object = None

    def __post_init__(self):
        if self.source not in ALLOWED_SOURCES:
            raise ValueError(f"trigger source not allowed: {self.source}")
        if not self.received_at:
            self.received_at = datetime.now().isoformat(timespec="seconds")

    def send_reply(self, text: str) -> None:
        if self.reply is not None:
            self.reply(text)


class TriggerDispatcher:
    """Route a TriggerEvent to a handler, by source."""

    def __init__(self):
        self.handlers: dict[str, object] = {}

    def register(self, source: str, handler) -> None:
        if source not in ALLOWED_SOURCES:
            raise ValueError(f"trigger source not allowed: {source}")
        self.handlers[source] = handler

    def dispatch(self, event: TriggerEvent):
        handler = self.handlers.get(event.source)
        if handler is None:
            raise ValueError(f"no handler for trigger source: {event.source}")
        return handler(event)


def handle_webhook(name: str, payload: dict, project_id: str = "default") -> TriggerEvent:
    """n8n-style webhook trigger, as a TriggerEvent."""
    goal = str(payload.get("goal", "")) if isinstance(payload, dict) else ""
    return TriggerEvent(source="webhook", kind=name, goal=goal,
                        project_id=project_id, payload=dict(payload or {}))


@dataclass
class SchedulerJob:
    name: str
    schedule: str
    goal: str
    project_id: str = "default"
    last_run: str = ""

    def next_due(self, after: datetime) -> datetime | None:
        parts = self.schedule.strip().split()
        if not parts:
            return None
        try:
            if parts[0] == "daily" and len(parts) == 2:
                hour, minute = (int(v) for v in parts[1].split(":"))
                candidate = datetime.combine(after.date(), time(hour, minute))
                if candidate <= after:
                    candidate += timedelta(days=1)
                return candidate
            if parts[0] == "weekly" and len(parts) == 3:
                weekday = WEEKDAYS[parts[1]]
                hour, minute = (int(v) for v in parts[2].split(":"))
                days_ahead = (weekday - after.weekday()) % 7
                candidate = datetime.combine(
                    after.date() + timedelta(days=days_ahead), time(hour, minute))
                if candidate <= after:
                    candidate += timedelta(days=7)
                return candidate
            if parts[0] == "monthly" and len(parts) == 4 and parts[1] == "first":
                weekday = WEEKDAYS[parts[2]]
                hour, minute = (int(v) for v in parts[3].split(":"))
                for year, month in ((after.year, after.month),
                                    (after.year + 1, 1) if after.month == 12
                                    else (after.year, after.month + 1)):
                    first = datetime(year, month, 1)
                    days_ahead = (weekday - first.weekday()) % 7
                    candidate = datetime.combine(
                        first.date() + timedelta(days=days_ahead),
                        time(hour, minute))
                    if candidate > after:
                        return candidate
                return None
            if parts[0] == "monthly" and len(parts) == 4 and ":" in parts[1]:
                hour, minute = (int(v) for v in parts[1].split(":"))
                day = int(parts[3])
                for year, month in ((after.year, after.month),
                                    (after.year + 1, 1) if after.month == 12
                                    else (after.year, after.month + 1)):
                    if day > 28:
                        continue
                    candidate = datetime(year, month, day, hour, minute)
                    if candidate > after:
                        return candidate
                return None
            if parts[0] == "cron" and len(parts) == 6:
                _, minute_s, hour_s, dom_s, _mon_s, dow_s = parts
                # Simple cron: * or a single number per field.
                # Search forward, at most 366 days, minute by minute
                # is too slow in tests, so step by day then hour then minute.
                candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
                for _ in range(366 * 24 * 60):
                    if (minute_s == "*" or int(minute_s) == candidate.minute) and \
                       (hour_s == "*" or int(hour_s) == candidate.hour) and \
                       (dom_s == "*" or int(dom_s) == candidate.day) and \
                       (dow_s == "*" or int(dow_s) == candidate.weekday()):
                        return candidate
                    candidate += timedelta(minutes=1)
                return None
        except (ValueError, KeyError):
            return None
        return None


class Scheduler:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.state_path = self.home / "scheduler.json"
        self.jobs: list[SchedulerJob] = []
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.jobs = [SchedulerJob(**job) for job in data.get("jobs", [])]
        except (json.JSONDecodeError, TypeError):
            self.jobs = []

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"jobs": [job.__dict__ for job in self.jobs]}, indent=2),
            encoding="utf-8")

    def add_job(self, job: SchedulerJob) -> None:
        self.jobs = [j for j in self.jobs if j.name != job.name]
        self.jobs.append(job)
        self.save()

    def check_due(self, now: datetime) -> list[SchedulerJob]:
        """Jobs due now, with last run updated so they do not double-fire."""
        due: list[SchedulerJob] = []
        for job in self.jobs:
            after = datetime.fromisoformat(job.last_run) if job.last_run else datetime(2000, 1, 1)
            nxt = job.next_due(after)
            if nxt is not None and nxt <= now:
                due.append(job)
                job.last_run = now.isoformat(timespec="seconds")
        if due:
            self.save()
        return due


class IngestWatcher:
    def __init__(self, ingest_dir: Path, home: Path):
        self.ingest_dir = Path(ingest_dir)
        self.state_path = Path(home) / "ingest_seen.json"
        self.seen: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            self.seen = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, TypeError):
            self.seen = {}

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self.seen, indent=2), encoding="utf-8")

    def scan(self) -> list[Path]:
        """New or changed files in the ingest folder, user files untouched."""
        if not self.ingest_dir.exists():
            return []
        new: list[Path] = []
        for path in sorted(self.ingest_dir.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            key = f"{stat.st_mtime_ns}:{stat.st_size}"
            if self.seen.get(str(path)) != key:
                new.append(path)
                self.seen[str(path)] = key
        if new:
            self.save()
        return new

    def events(self, project_id: str = "default") -> list[TriggerEvent]:
        return [TriggerEvent(source="ingest", kind="file",
                             goal=f"Ingest {path.name}",
                             project_id=project_id,
                             payload={"file": path.name, "path": str(path)})
                for path in self.scan()]
