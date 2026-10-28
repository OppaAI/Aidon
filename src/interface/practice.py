"""Free-time practice: scheduled windows that run goals until a pass.

A practice schedule names a window — a date and/or days of
week plus a start and end time — and a list of goals (each
optionally naming a spec). While the window is open, Aidon
picks goals at random and runs each one exactly like a
usual run, so a successful run learns its workflow (DAG)
into memory like any other success. The session ends at
the first successful pass, or when the window closes,
whichever comes first; failed attempts are retried with a
fresh random pick until then. Every successful run gets a
practice report (WebUI, Slack, WhatsApp); a window that
closes with no pass gets a summary in the WebUI record
only, so a silent failure never looks like a pass.

Practice runs are unattended by definition — free time
means the owner is away — so the practice gate
auto-approves: creating the schedule IS the owner's
approval for those goals in that window, and the report
says so. A practice run that would send mail, spend
money, or publish should not be scheduled; the gate does
not second-guess the schedule.

Storage is one JSON file under the Aidon home
(practice.json), separate from the cron-style scheduler:
a practice window is a duration, not a moment, so it does
not fit a fire-once job. The serve daemon ticks the
schedules once a minute and starts at most one session
per schedule per day.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, time, timedelta
from pathlib import Path

_PRACTICE_FILE = "practice.json"
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MAX_SESSIONS_KEPT = 20


def _parse_hhmm(text: str) -> time | None:
    try:
        hour_text, minute_text = str(text or "").strip().split(":")
        hour, minute = int(hour_text), int(minute_text)
    except (ValueError, AttributeError):
        return None
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return time(hour, minute)


def _parse_days(days) -> list[int] | None:
    """Weekday names or 0-6 ints (0 = Monday) to a sorted int list."""
    if days is None:
        return []
    out: set[int] = set()
    for item in days:
        if isinstance(item, int) and 0 <= item <= 6:
            out.add(item)
        elif isinstance(item, str):
            name = item.strip()[:3].title()
            if name in WEEKDAY_NAMES:
                out.add(WEEKDAY_NAMES.index(name))
            else:
                return None
        else:
            return None
    return sorted(out)


def validate_schedule(data: dict) -> tuple[dict | None, str | None]:
    """Validate raw schedule data; returns (schedule, error)."""
    if not isinstance(data, dict):
        return None, "schedule must be an object"
    name = str(data.get("name", "") or "").strip()
    if not name:
        return None, "a name is required"
    start = _parse_hhmm(data.get("start", ""))
    end = _parse_hhmm(data.get("end", ""))
    if start is None or end is None:
        return None, "start and end must be HH:MM times"
    if start >= end:
        return None, "start must be before end"
    date = str(data.get("date", "") or "").strip()
    if date:
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return None, "date must be YYYY-MM-DD"
    days = _parse_days(data.get("days", []))
    if days is None:
        return None, "days must be weekday names (Mon..Sun)"
    goals = []
    for item in data.get("goals", []) or []:
        if isinstance(item, str):
            if item.strip():
                goals.append({"goal": item.strip(), "spec": ""})
        elif isinstance(item, dict) and str(item.get("goal", "")).strip():
            goals.append({"goal": str(item["goal"]).strip(),
                          "spec": str(item.get("spec", "") or "").strip()})
    if not goals:
        return None, "at least one goal is required"
    return {
        "name": name,
        "date": date,
        "days": days,
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
        "goals": goals,
        "created": datetime.now().isoformat(timespec="seconds"),
        "last_session_date": "",
        "sessions": [],
    }, None


def _path(home: Path) -> Path:
    return Path(home) / _PRACTICE_FILE


def load_schedules(home: Path) -> list[dict]:
    """All practice schedules, [] when the file is absent or broken."""
    try:
        data = json.loads(_path(home).read_text(encoding="utf-8"))
    except Exception:
        return []
    schedules = data.get("schedules", []) if isinstance(data, dict) else []
    return [s for s in schedules if isinstance(s, dict)]


def save_schedules(home: Path, schedules: list[dict]) -> None:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"schedules": schedules}, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def add_schedule(home: Path, data: dict) -> tuple[dict | None, str | None]:
    """Validate and store a schedule; names are unique."""
    schedule, error = validate_schedule(data)
    if error is not None:
        return None, error
    schedules = load_schedules(home)
    if any(s.get("name") == schedule["name"] for s in schedules):
        return None, f"a practice schedule named {schedule['name']!r} exists"
    schedules.append(schedule)
    save_schedules(home, schedules)
    return schedule, None


def remove_schedule(home: Path, name: str) -> bool:
    """Delete a schedule by name; True when one existed."""
    schedules = load_schedules(home)
    kept = [s for s in schedules if s.get("name") != name]
    if len(kept) == len(schedules):
        return False
    save_schedules(home, kept)
    return True


def get_schedule(home: Path, name: str) -> dict | None:
    for schedule in load_schedules(home):
        if schedule.get("name") == name:
            return schedule
    return None


def window_active(schedule: dict, now: datetime) -> bool:
    """True when now falls inside the schedule's window.

    A set date matches that date only; otherwise set days
    match those weekdays; neither set means every day.
    """
    date = str(schedule.get("date", "") or "")
    if date:
        if date != now.strftime("%Y-%m-%d"):
            return False
    elif schedule.get("days"):
        if now.weekday() not in [int(d) for d in schedule["days"]]:
            return False
    start = _parse_hhmm(schedule.get("start", ""))
    end = _parse_hhmm(schedule.get("end", ""))
    if start is None or end is None:
        return False
    return start <= now.time() < end


def due_for_session(schedule: dict, now: datetime) -> bool:
    """True when the window is open and no session started today."""
    return (window_active(schedule, now)
            and schedule.get("last_session_date") != now.strftime("%Y-%m-%d"))


def window_end(schedule: dict, day: datetime) -> datetime:
    """The window's end datetime for a given session day."""
    end = _parse_hhmm(schedule.get("end", "")) or time(0, 0)
    return datetime.combine(day.date(), end)


def load_spec(home: Path, name: str) -> dict | None:
    """A frozen interview spec by project name, None when absent."""
    if not name:
        return None
    path = Path(home) / "specs" / (name if name.endswith(".json") else name + ".json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def practice_report_text(schedule_name: str, goal: str, result: dict,
                         started: str, ended: str) -> str:
    """The report sent for each successful practice run."""
    files = ", ".join(result.get("files", []) or []) or "no files"
    return (
        f"Practice report: {schedule_name}\n"
        f"Goal: {goal}\n"
        f"Status: passed\n"
        f"Files: {files}\n"
        f"Run: {result.get('run_id', '')}\n"
        f"Started: {started}\n"
        f"Ended: {ended}\n"
        f"Workflow: {'learned' if result.get('dag') else 'no DAG recorded'}\n"
        f"(Practice runs are auto-approved by the schedule itself.)"
    )


def run_practice_session(schedule: dict, run_goal, end: datetime,
                         home=None, now_fn=datetime.now, rng=None,
                         on_report=None) -> dict:
    """Run random goals until a pass or the window end.

    run_goal(goal_text, spec_dict) returns a result dict with
    status/files/error/elapsed_seconds/tokens/run_id/dag.
    Specs are loaded from <home>/specs/<name>.json when the
    picked goal names one; a missing spec runs without one
    and the attempt record keeps the name, so the gap shows.
    on_report(report_text, goal, result) is called for each
    successful run — in practice the session ends at the
    first success, so that is one report per session.
    """
    rng = rng or random
    goals = schedule.get("goals", [])
    attempts: list[dict] = []
    started = now_fn().isoformat(timespec="seconds")
    success = None
    while now_fn() < end and goals:
        pick = rng.choice(goals)
        spec_name = str(pick.get("spec", "") or "")
        spec = load_spec(home, spec_name) if home is not None else None
        result = run_goal(str(pick["goal"]), spec)
        record = {"goal": str(pick["goal"]), "spec": spec_name,
                  "status": str(result.get("status", "")),
                  "files": list(result.get("files", []) or []),
                  "error": str(result.get("error", "") or "")[:300],
                  "run_id": str(result.get("run_id", "") or "")}
        attempts.append(record)
        if record["status"] == "done":
            success = record
            ended = now_fn().isoformat(timespec="seconds")
            if on_report is not None:
                on_report(practice_report_text(
                    schedule["name"], record["goal"], result, started, ended),
                    record["goal"], result)
            break
    ended = now_fn().isoformat(timespec="seconds")
    return {
        "date": started[:10],
        "started": started,
        "ended": ended,
        "passed": success is not None,
        "goal": success["goal"] if success else "",
        "attempts": attempts,
        "run_id": success["run_id"] if success else "",
    }


def record_session(home: Path, schedule_name: str, summary: dict) -> None:
    """Append a session summary to its schedule, capped at 20."""
    schedules = load_schedules(home)
    for schedule in schedules:
        if schedule.get("name") == schedule_name:
            sessions = schedule.get("sessions", []) or []
            sessions.append(summary)
            schedule["sessions"] = sessions[-_MAX_SESSIONS_KEPT:]
            break
    save_schedules(home, schedules)
