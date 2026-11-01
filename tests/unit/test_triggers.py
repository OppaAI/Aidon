"""Triggers: scheduler, ingest folder, webhook, dispatcher, no Telegram.

All trigger sources produce the same TriggerEvent shape,
and a trigger never bypasses approval, it only starts a run.
"""

from datetime import datetime

import pytest

from src.interface.triggers import (ALLOWED_SOURCES, IngestWatcher, Scheduler,
                          SchedulerJob, TriggerDispatcher, TriggerEvent,
                          handle_webhook)


def test_scheduler_first_wednesday_3pm():
    job = SchedulerJob(name="statements", schedule="monthly first Wed 15:00",
                       goal="Send statements")
    # After Oct 31, next is Nov 4, 2026, first Wednesday, 3pm.
    # That is the send time for October statements.
    nxt = job.next_due(datetime(2026, 10, 31, 0, 0))
    assert nxt == datetime(2026, 11, 4, 15, 0)
    nxt2 = job.next_due(datetime(2026, 11, 4, 15, 1))
    assert nxt2 == datetime(2026, 12, 2, 15, 0)


def test_scheduler_daily_weekly_and_due_no_double_fire(tmp_path):
    scheduler = Scheduler(tmp_path / "home")
    scheduler.add_job(SchedulerJob(name="daily", schedule="daily 09:00",
                                   goal="Daily run"))
    due = scheduler.check_due(datetime(2026, 10, 26, 9, 0))
    assert [j.name for j in due] == ["daily"]
    # A claimed occurrence is replayed until the caller acknowledges it.
    assert [j.name for j in scheduler.check_due(
        datetime(2026, 10, 26, 9, 1))] == ["daily"]
    assert scheduler.complete(due[0]) is True
    # Same occurrence does not fire again.
    due2 = scheduler.check_due(datetime(2026, 10, 26, 9, 1))
    assert due2 == []


def test_scheduler_cron_simple(tmp_path):
    job = SchedulerJob(name="cron", schedule="cron 0 15 * * *",
                       goal="Cron run")
    assert job.next_due(datetime(2026, 10, 26, 14, 0)) == datetime(2026, 10, 26, 15, 0)


def test_scheduler_preserves_job_kind_and_validates_chain(tmp_path):
    scheduler = Scheduler(tmp_path / "home")
    scheduler.add_job(SchedulerJob(name="learning", kind="learn",
                                   schedule="0 9 * * *", goal=""))
    assert scheduler.jobs[0].kind == "learn"
    with pytest.raises(ValueError, match="invalid chain"):
        scheduler.add_job(SchedulerJob(name="bad", kind="chain",
                                       schedule="0 9 * * *", goal="{}"))


def test_ingest_watcher_new_changed_unchanged(tmp_path):
    ingest = tmp_path / "ingest"
    ingest.mkdir()
    (ingest / "a.txt").write_text("hello", encoding="utf-8")
    watcher = IngestWatcher(ingest, tmp_path / "home")
    first = watcher.scan()
    assert [p.name for p in first] == ["a.txt"]
    assert watcher.scan() == []
    (ingest / "a.txt").write_text("changed", encoding="utf-8")
    assert [p.name for p in watcher.scan()] == ["a.txt"]
    events = watcher.events()
    assert events == []


def test_dispatcher_whatsapp_slack_two_way_and_no_telegram():
    dispatcher = TriggerDispatcher()
    replies = []
    dispatcher.register("whatsapp", lambda e: e.send_reply("got it"))
    dispatcher.register("slack", lambda e: e.send_reply("got it"))
    event = TriggerEvent(source="whatsapp", goal="hi", reply=replies.append)
    dispatcher.dispatch(event)
    assert replies == ["got it"]
    with pytest.raises(ValueError):
        TriggerEvent(source="telegram", goal="hi")
    with pytest.raises(ValueError):
        dispatcher.register("telegram", lambda e: None)


def test_webhook_trigger_is_n8n_style():
    event = handle_webhook("invoice-arrived", {"goal": "Process invoice"})
    assert event.source == "webhook"
    assert event.kind == "invoice-arrived"
    assert event.goal == "Process invoice"
    assert "telegram" not in ALLOWED_SOURCES
    assert "whatsapp" in ALLOWED_SOURCES and "slack" in ALLOWED_SOURCES
