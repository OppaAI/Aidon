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
conversation id. Schedule strings supported:
- "daily HH:MM"
- "weekly Mon HH:MM" (Mon, Tue, Wed, Thu, Fri, Sat, Sun)
- "monthly first Wed HH:MM" (first weekday of the month)
- "monthly HH:MM day D" (day of month, 1 to 28)
- "cron M H DOM MON DOW" (5 fields, * or a single number)
next_due(after) returns the next occurrence after a
time, in local time, or in the job's tz when one is set. check_due(now) returns jobs whose
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
    conversation_id: str = ""
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


def handle_webhook(name: str, payload: dict, conversation_id: str = "") -> TriggerEvent:
    """n8n-style webhook trigger, as a TriggerEvent."""
    goal = str(payload.get("goal", "")) if isinstance(payload, dict) else ""
    return TriggerEvent(source="webhook", kind=name, goal=goal,
                        conversation_id=conversation_id, payload=dict(payload or {}))


def _cron_field(text: str, low: int, high: int,
                *, dow: bool = False) -> set[int]:
    """Parse one cron field: ``*``, ``5``, ``1-5``, ``*/15``, ``1,3,5``.

    Raises ValueError on anything outside the field's range, so a
    typo fails at creation instead of silently never firing.
    """
    values: set[int] = set()
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field")
        step = 1
        if "/" in part:
            part, step_text = part.split("/", 1)
            step = int(step_text)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            start_text, end_text = part.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(part)
        if start < low or end > high or start > end:
            raise ValueError(
                f"cron field {text!r} outside {low}-{high}")
        values.update(range(start, end + 1, step))
    if dow:
        # cron accepts both 0 and 7 for Sunday.
        values = {0 if value == 7 else value for value in values}
    if not values:
        raise ValueError(f"empty cron field {text!r}")
    return values


def _next_cron(expr: str, after: datetime) -> datetime | None:
    """Next datetime strictly after ``after`` for a 5-field cron.

    Accepts both ``M H DOM MON DOW`` and the older
    ``cron M H DOM MON DOW`` spelling. Day-of-month and
    day-of-week follow cron: when both are restricted, either
    may match. Month is always AND. The old implementation
    ignored month, used Python's Monday=0 weekday for a cron
    Sunday=0 field, and only accepted single numbers, so
    ``0 9 * * *`` (the CLI default) never fired at all.
    """
    parts = expr.strip().split()
    if len(parts) == 6 and parts[0].lower() == "cron":
        parts = parts[1:]
    if len(parts) != 5:
        raise ValueError(
            "cron needs 5 fields (minute hour dom month dow)")
    minutes = _cron_field(parts[0], 0, 59)
    hours = _cron_field(parts[1], 0, 23)
    days = _cron_field(parts[2], 1, 31)
    months = _cron_field(parts[3], 1, 12)
    weekdays = _cron_field(parts[4], 0, 7, dow=True)
    dom_any = parts[2].strip() == "*"
    dow_any = parts[4].strip() == "*"

    candidate = (after + timedelta(minutes=1)).replace(
        second=0, microsecond=0)
    # Every valid yearly calendar pattern occurs within this span; an
    # eight-year horizon covers the full leap-day gap without scanning
    # the entire 400-year Gregorian cycle.
    limit = candidate + timedelta(days=8 * 366)
    while candidate <= limit:
        if candidate.month not in months:
            year, month = (candidate.year + 1, 1) \
                if candidate.month == 12 else (candidate.year,
                                               candidate.month + 1)
            candidate = datetime(year, month, 1, tzinfo=after.tzinfo)
            continue
        weekday = (candidate.weekday() + 1) % 7  # cron: Sunday = 0
        if dom_any and dow_any:
            day_ok = True
        elif dom_any:
            day_ok = weekday in weekdays
        elif dow_any:
            day_ok = candidate.day in days
        else:
            day_ok = candidate.day in days or weekday in weekdays
        if not day_ok:
            candidate = (candidate + timedelta(days=1)).replace(
                hour=0, minute=0)
            continue
        if candidate.hour not in hours:
            candidate = (candidate + timedelta(hours=1)).replace(minute=0)
            continue
        if candidate.minute not in minutes:
            candidate += timedelta(minutes=1)
            continue
        return candidate
    return None


@dataclass
class SchedulerJob:
    name: str
    schedule: str
    goal: str
    conversation_id: str = ""
    last_run: str = ""
    # Kept for compatibility with the old registry scheduler.  A missing
    # field in an existing scheduler.json is a normal goal job.  This is
    # placed last so existing positional construction keeps its meaning.
    kind: str = "goal"
    # Expiry: ISO datetime; the job never fires after it.
    until: str = ""
    # IANA timezone for interpreting the schedule; "" keeps the
    # historical server-local naive behavior.
    tz: str = ""
    # Wall-clock time limit per run, seconds; 0 = unlimited.
    time_limit: int = 0

    def next_due(self, after: datetime) -> datetime | None:
        """Next run after `after`, capped by expiry (until).

        A naive `until` reads as wall time in the job zone (or
        server-local when no zone); an aware `until` compares
        directly. A candidate past `until` never fires.
        """
        candidate = self._next_due_uncapped(after)
        if candidate is None or not self.until.strip():
            return candidate
        try:
            until = datetime.fromisoformat(self.until.strip())
        except ValueError:
            return None
        if candidate.tzinfo is None and until.tzinfo is not None:
            until = until.astimezone().replace(tzinfo=None)
        elif candidate.tzinfo is not None and until.tzinfo is None:
            until = until.replace(tzinfo=candidate.tzinfo)
        return candidate if candidate <= until else None

    def _next_due_uncapped(self, after: datetime) -> datetime | None:
        schedule = self.schedule.strip()
        if not schedule:
            return None
        zone = None
        if self.tz.strip():
            try:
                from zoneinfo import ZoneInfo
                zone = ZoneInfo(self.tz.strip())
            except Exception:
                return None
            # Naive `after` (last_run is stored naive) reads as
            # wall time in the job zone.
            after = after.replace(tzinfo=zone) if after.tzinfo is None \
                else after.astimezone(zone)

        def _zoned(dt):
            return dt.replace(tzinfo=zone) \
                if zone is not None and dt.tzinfo is None else dt

        if schedule.startswith("at:"):
            # One-shot: fire once at the given ISO time, never again.
            # A naive one-shot reads as wall time in the job zone.
            try:
                when = datetime.fromisoformat(schedule[3:].strip())
            except ValueError:
                return None
            when = _zoned(when)
            return when if when > after else None
        parts = schedule.split()
        try:
            if parts[0] == "daily" and len(parts) == 2:
                hour, minute = (int(v) for v in parts[1].split(":"))
                candidate = _zoned(datetime.combine(after.date(), time(hour, minute)))
                if candidate <= after:
                    candidate += timedelta(days=1)
                return candidate
            if parts[0] == "weekly" and len(parts) == 3:
                weekday = WEEKDAYS[parts[1]]
                hour, minute = (int(v) for v in parts[2].split(":"))
                days_ahead = (weekday - after.weekday()) % 7
                candidate = _zoned(datetime.combine(
                    after.date() + timedelta(days_ahead), time(hour, minute)))
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
                    candidate = _zoned(datetime.combine(
                        first.date() + timedelta(days=days_ahead),
                        time(hour, minute)))
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
                    candidate = _zoned(datetime(year, month, day, hour, minute))
                    if candidate > after:
                        return candidate
                return None
            return _next_cron(schedule, after)
        except (ValueError, KeyError, TypeError):
            return None


class Scheduler:
    KINDS = ("goal", "chain", "learn", "report", "brief")

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
            self.jobs = [SchedulerJob(**{
                ("conversation_id" if k == "project_id" else k): v
                for k, v in job.items()
            }) for job in data.get("jobs", [])]
        except (json.JSONDecodeError, TypeError):
            self.jobs = []

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"jobs": [job.__dict__ for job in self.jobs]}, indent=2),
            encoding="utf-8")

    def add_job(self, job: SchedulerJob) -> None:
        if job.kind not in self.KINDS:
            raise ValueError(
                f"job kind must be {'|'.join(self.KINDS)}, got {job.kind!r}")
        if not job.goal.strip() and job.kind == "goal":
            raise ValueError("goal jobs need goal text")
        if job.kind == "chain":
            try:
                spec = json.loads(job.goal)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"chain jobs need JSON in goal: {exc}") from exc
            from ..capabilities.chain import (
                validate_chain_spec,
                validate_conditional_chain,
            )
            if isinstance(spec, list):
                failures = validate_conditional_chain(spec)
            elif isinstance(spec, dict):
                failures = validate_chain_spec(spec)
            else:
                failures = ["chain goal must be a JSON object or array"]
            if failures:
                raise ValueError("invalid chain: " + "; ".join(failures))
        # A schedule that never fires is a silent dead job: the
        # daemon only understands next_due forms (at:/daily/
        # weekly/monthly/cron). "every …" and bare ISO datetimes
        # parse elsewhere but never fire, so reject them here,
        # where the creator sees the failure and can retry with
        # a firing form. A one-shot in the past never fires either.
        if job.until.strip():
            try:
                datetime.fromisoformat(job.until.strip())
            except ValueError:
                raise ValueError(f"until {job.until!r} is not an ISO datetime")
        if job.tz.strip():
            try:
                from zoneinfo import ZoneInfo
                ZoneInfo(job.tz.strip())
            except Exception:
                raise ValueError(f"unknown timezone {job.tz!r}")
        try:
            job.time_limit = int(job.time_limit or 0)
        except (TypeError, ValueError):
            raise ValueError(f"time_limit {job.time_limit!r} is not seconds")
        if job.time_limit < 0:
            raise ValueError("time_limit must be 0 (unlimited) or positive")
        if job.next_due(datetime.now()) is None:
            raise ValueError(
                f"schedule {job.schedule!r} never fires; use at:<ISO datetime>, "
                "daily HH:MM, weekly DAY HH:MM, monthly ..., or a 5-field cron")
        self.jobs = [j for j in self.jobs if j.name != job.name]
        self.jobs.append(job)
        self.save()

    def due_jobs(self, now: datetime) -> list[SchedulerJob]:
        """Return due jobs without changing their persisted last-run time."""
        return self._due_jobs(now, mark=False)

    def check_due(self, now: datetime) -> list[SchedulerJob]:
        """Return due jobs and record them as claimed for this tick.

        A zoned job's next run is timezone-aware; a naive `now`
        reads as server-local for the comparison.
        """
        return self._due_jobs(now, mark=True)

    def _due_jobs(self, now: datetime, *, mark: bool) -> list[SchedulerJob]:
        due: list[SchedulerJob] = []
        for job in self.jobs:
            after = datetime.fromisoformat(job.last_run) if job.last_run else datetime(2000, 1, 1)
            nxt = job.next_due(after)
            if nxt is None:
                continue
            now_cmp = now
            if nxt.tzinfo is not None and now.tzinfo is None:
                now_cmp = now.astimezone(nxt.tzinfo)
            if nxt <= now_cmp:
                due.append(job)
                if mark:
                    job.last_run = now.isoformat(timespec="seconds")
        if due and mark:
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

    def events(self, conversation_id: str = "") -> list[TriggerEvent]:
        return [TriggerEvent(source="ingest", kind="file",
                             goal=f"Ingest {path.name}",
                             conversation_id=conversation_id,
                             payload={"file": path.name, "path": str(path)})
                for path in self.scan()]


# ── Scheduler depth: cron parsing, next runs, schedule forms ──
# These are new public helpers. They do not change Scheduler or
# SchedulerJob above, which keep their own naive local-time
# behaviour. These helpers are timezone-aware throughout.

def parse_cron(expr: str) -> dict:
    """Parse a 5-field cron expression into sets of values.

    Accepts an optional leading "cron " word. Day-of-week
    follows cron: Sunday is 0, and 7 is accepted as Sunday
    and normalised to 0. Raises ValueError on the wrong
    field count or any value outside its range.
    """
    parts = expr.strip().split()
    if len(parts) == 6 and parts[0].lower() == "cron":
        parts = parts[1:]
    if len(parts) != 5:
        raise ValueError(
            f"cron needs 5 fields (minute hour dom month dow), got {len(parts)}")
    return {
        "minute": _cron_field(parts[0], 0, 59),
        "hour": _cron_field(parts[1], 0, 23),
        "dom": _cron_field(parts[2], 1, 31),
        "month": _cron_field(parts[3], 1, 12),
        "dow": _cron_field(parts[4], 0, 7, dow=True),
    }


def _cron_matches(cron: dict, dom_any: bool, dow_any: bool, moment: datetime) -> bool:
    if moment.minute not in cron["minute"]:
        return False
    if moment.hour not in cron["hour"]:
        return False
    if moment.month not in cron["month"]:
        return False
    weekday = (moment.weekday() + 1) % 7  # cron: Sunday = 0
    if dom_any and dow_any:
        day_ok = True
    elif dom_any:
        day_ok = weekday in cron["dow"]
    elif dow_any:
        day_ok = moment.day in cron["dom"]
    else:
        day_ok = moment.day in cron["dom"] or weekday in cron["dow"]
    return day_ok


def _zone(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        from datetime import timezone
        return timezone.utc


def next_cron_run(expr: str, from_dt: datetime | None = None,
                  tz: str = "UTC") -> datetime:
    """Next run strictly after from_dt for a cron expression.

    Cron fields are interpreted in tz (default UTC). The
    result is timezone-aware in tz. Raises ValueError when
    no run falls within the next year.
    """
    from datetime import timezone
    zone = _zone(tz)
    parts = expr.strip().split()
    if len(parts) == 6 and parts[0].lower() == "cron":
        parts = parts[1:]
    cron = parse_cron(expr)
    dom_any = parts[2].strip() == "*"
    dow_any = parts[4].strip() == "*"
    base = from_dt or datetime.now(timezone.utc)
    moment = base.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60):
        if _cron_matches(cron, dom_any, dow_any, moment):
            return moment
        moment += timedelta(minutes=1)
    raise ValueError(f"no run within a year for cron {expr!r}")


def is_oneshot(schedule: str) -> bool:
    """True for a one-time schedule: "at:<ISO datetime>" or a
    bare ISO datetime. Cron expressions and "every ..." are
    recurring, not oneshot.
    """
    text = (schedule or "").strip()
    if text.startswith("at:"):
        return True
    if text.startswith(("every ", "daily", "weekly", "monthly", "cron ")):
        return False
    if len(text.split()) == 5 and all(
            all(ch.isdigit() or ch in "*/,-" for ch in part) for part in text.split()):
        return False
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def parse_schedule(schedule: str, from_dt: datetime | None = None,
                   tz: str = "UTC") -> datetime:
    """Next run for a schedule string, timezone-aware in tz.

    Accepted forms:
    - cron expression, with or without a leading "cron "
    - "at:<ISO datetime>" (naive datetimes are read in tz)
    - a bare ISO datetime, same oneshot meaning as "at:"
    - "every N minutes", "every N hours", "every N days"
    Anything else raises ValueError.
    """
    from datetime import timezone
    zone = _zone(tz)
    text = (schedule or "").strip()
    if not text:
        raise ValueError("empty schedule")
    if text.startswith("at:"):
        moment = datetime.fromisoformat(text[3:].strip())
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        return moment.astimezone(zone)
    if is_oneshot(text):
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        return moment.astimezone(zone)
    if text.startswith("every "):
        rest = text[6:].strip().split()
        if len(rest) != 2:
            raise ValueError(f"bad relative schedule: {schedule!r}")
        amount = int(rest[0])
        unit = rest[1].lower().rstrip("s")
        if unit == "minute":
            delta = timedelta(minutes=amount)
        elif unit == "hour":
            delta = timedelta(hours=amount)
        elif unit == "day":
            delta = timedelta(days=amount)
        else:
            raise ValueError(f"unknown relative unit: {rest[1]!r}")
        base = from_dt or datetime.now(timezone.utc)
        return base.astimezone(zone) + delta
    return next_cron_run(text, from_dt, tz)
