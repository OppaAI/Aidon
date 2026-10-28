"""Practice schedules, windows, sessions, and reports."""

import json
from datetime import datetime, timedelta

from src.interface.practice import (
    add_schedule, due_for_session, get_schedule, load_schedules,
    load_spec, practice_report_text, record_session, remove_schedule,
    run_practice_session, validate_schedule, window_active, window_end)


def _sched(**over):
    data = {"name": "evening", "days": ["Mon", "Wed"], "start": "21:00",
            "end": "22:00", "goals": [{"goal": "Write a haiku", "spec": ""}]}
    data.update(over)
    return data


def test_validate_schedule():
    schedule, error = validate_schedule(_sched())
    assert error is None and schedule["days"] == [0, 2]
    assert schedule["start"] == "21:00" and schedule["end"] == "22:00"
    _, error = validate_schedule(_sched(start="22:00", end="21:00"))
    assert error and "before end" in error
    _, error = validate_schedule(_sched(start="9pm"))
    assert error
    _, error = validate_schedule(_sched(goals=[]))
    assert error
    _, error = validate_schedule(_sched(date="2026-13-01"))
    assert error
    _, error = validate_schedule(_sched(days=["Funday"]))
    assert error
    schedule, error = validate_schedule(_sched(
        goals=[{"goal": "Do it", "spec": "dinner"}, "Plain goal"]))
    assert error is None
    assert schedule["goals"] == [{"goal": "Do it", "spec": "dinner"},
                                 {"goal": "Plain goal", "spec": ""}]


def test_window_active():
    # 2026-11-02 is a Monday.
    monday = datetime(2026, 11, 2, 21, 30)
    schedule, _ = validate_schedule(_sched())
    assert window_active(schedule, monday) is True
    assert window_active(schedule, datetime(2026, 11, 2, 22, 0)) is False
    assert window_active(schedule, datetime(2026, 11, 2, 20, 59)) is False
    assert window_active(schedule, datetime(2026, 11, 3, 21, 30)) is False
    dated, _ = validate_schedule(_sched(date="2026-11-02", days=[]))
    assert window_active(dated, monday) is True
    assert window_active(dated, datetime(2026, 11, 9, 21, 30)) is False
    daily, _ = validate_schedule(_sched(days=[]))
    assert window_active(daily, datetime(2026, 11, 5, 21, 30)) is True


def test_due_for_session():
    monday = datetime(2026, 11, 2, 21, 30)
    schedule, _ = validate_schedule(_sched())
    assert due_for_session(schedule, monday) is True
    schedule["last_session_date"] = "2026-11-02"
    assert due_for_session(schedule, monday) is False


def test_session_ends_at_first_success(tmp_path):
    schedule, _ = validate_schedule(_sched(goals=[
        {"goal": "Fail first", "spec": ""}, {"goal": "Pass second", "spec": ""}]))
    calls = []

    def run_goal(goal, spec):
        calls.append(goal)
        if goal == "Pass second":
            return {"status": "done", "files": ["out.md"], "error": "",
                    "run_id": "r1", "dag": {"nodes": []}}
        return {"status": "failed", "files": [], "error": "nope",
                "run_id": "r0", "dag": {}}

    class Cycle:
        def __init__(self, items):
            self.items = items
            self.i = 0

        def choice(self, seq):
            item = self.items[self.i % len(self.items)]
            self.i += 1
            return item

    reports = []
    summary = run_practice_session(
        schedule, run_goal, datetime(2026, 11, 2, 22, 0),
        now_fn=lambda: datetime(2026, 11, 2, 21, 30),
        rng=Cycle([{"goal": "Fail first", "spec": ""},
                   {"goal": "Pass second", "spec": ""}]),
        on_report=lambda text, goal, result: reports.append(text))
    assert summary["passed"] is True and summary["goal"] == "Pass second"
    assert len(summary["attempts"]) == 2 and len(reports) == 1
    assert "evening" in reports[0] and "Pass second" in reports[0]


def test_session_stops_at_window_end():
    schedule, _ = validate_schedule(_sched())
    times = iter([datetime(2026, 11, 2, 21, 30),
                  datetime(2026, 11, 2, 21, 30),
                  datetime(2026, 11, 2, 22, 5),
                  datetime(2026, 11, 2, 22, 5)])
    reports = []
    summary = run_practice_session(
        schedule, lambda goal, spec: {"status": "failed", "files": []},
        datetime(2026, 11, 2, 22, 0), now_fn=lambda: next(times),
        on_report=lambda text, goal, result: reports.append(text))
    assert summary["passed"] is False and len(summary["attempts"]) == 1
    assert reports == []


def test_session_loads_specs(tmp_path):
    specs = tmp_path / "specs"
    specs.mkdir()
    (specs / "dinner.json").write_text(json.dumps({"deliverables": ["menu.pdf"]}))
    schedule, _ = validate_schedule(_sched(goals=[
        {"goal": "Plan dinner", "spec": "dinner"},
        {"goal": "No spec goal", "spec": "missing"}]))
    seen = {}

    class Cycle:
        def __init__(self, items):
            self.items = items
            self.i = 0

        def choice(self, seq):
            item = self.items[self.i % len(self.items)]
            self.i += 1
            return item

    def run_goal(goal, spec):
        seen[goal] = spec
        if goal == "Plan dinner":
            return {"status": "done", "files": [], "run_id": "r"}
        return {"status": "failed", "files": [], "run_id": "r0"}

    summary = run_practice_session(
        schedule, run_goal, datetime(2026, 11, 2, 22, 0),
        home=tmp_path, now_fn=lambda: datetime(2026, 11, 2, 21, 30),
        rng=Cycle([{"goal": "No spec goal", "spec": "missing"},
                   {"goal": "Plan dinner", "spec": "dinner"}]))
    assert summary["passed"] is True
    assert seen["Plan dinner"] == {"deliverables": ["menu.pdf"]}
    assert seen["No spec goal"] is None
    assert load_spec(tmp_path, "missing") is None


def test_store_round_trip_and_record(tmp_path):
    schedule, error = add_schedule(tmp_path, _sched())
    assert error is None
    assert get_schedule(tmp_path, "evening")["name"] == "evening"
    _, error = add_schedule(tmp_path, _sched())
    assert error and "exists" in error
    record_session(tmp_path, "evening", {"date": "2026-11-02", "passed": True,
                                         "attempts": []})
    assert len(get_schedule(tmp_path, "evening")["sessions"]) == 1
    assert load_schedules(tmp_path)[0]["name"] == "evening"
    assert remove_schedule(tmp_path, "evening") is True
    assert remove_schedule(tmp_path, "evening") is False


def test_report_text():
    text = practice_report_text("evening", "Write a haiku",
                                {"files": ["haiku.md"], "run_id": "r9",
                                 "dag": {"nodes": []}},
                                "2026-11-02T21:00:00", "2026-11-02T21:05:00")
    assert "evening" in text and "Write a haiku" in text
    assert "haiku.md" in text and "auto-approved" in text
