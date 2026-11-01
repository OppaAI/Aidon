"""Outbox cadence, tick draining, and scheduled-run notification."""

import json
import multiprocessing
import queue
import time
from datetime import datetime, timedelta, timezone

from src.capabilities.outbox import Outbox
from src.interface.scheduler_loop import SchedulerLoop

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


def _queue_many(home, count):
    outbox = Outbox(home)
    for index in range(count):
        outbox.queue("a@example.com", str(index), "body")


def _drain_after_barrier(home, barrier, sent_ids):
    outbox = Outbox(home)
    barrier.wait()

    def sender(item):
        sent_ids.put(item["id"])
        time.sleep(0.05)
        return True

    outbox.drain(sender)


def _set_next_attempt_at(outbox, item_id, when):
    items = outbox.list()
    for item in items:
        if item["id"] == item_id:
            item["next_attempt_at"] = when.isoformat()
    outbox._save(items)


class _FakeScheduler:
    def __init__(self, due):
        self.due = due

    def check_due(self, now):
        return list(self.due)


class _RecordingDispatcher:
    def __init__(self, order):
        self.order = order

    def dispatch(self, event):
        self.order.append(("dispatch", event["goal"]))


def test_drain_attempts_only_due_items(tmp_path):
    outbox = Outbox(tmp_path)
    first = outbox.queue("a@example.com", "one", "body")
    second = outbox.queue("b@example.com", "two", "body")
    _set_next_attempt_at(outbox, first["id"], T0)
    _set_next_attempt_at(outbox, second["id"], T0 + timedelta(hours=1))
    attempted = []
    result = outbox.drain(lambda item: attempted.append(item["id"]) or True, now=T0)
    assert attempted == [first["id"]]
    assert result == {"sent": 1, "failed": 0, "remaining": 1}


def test_failed_attempt_sets_attempts_and_next_attempt(tmp_path):
    outbox = Outbox(tmp_path)
    item0 = outbox.queue("a@example.com", "one", "body")
    _set_next_attempt_at(outbox, item0["id"], T0)
    result = outbox.drain(lambda item: False, now=T0)
    assert result["failed"] == 1 and result["remaining"] == 1
    item = outbox.list()[0]
    assert item["attempts"] == 1
    assert item["status"] == "failed"
    assert datetime.fromisoformat(item["next_attempt_at"]) == T0 + timedelta(seconds=60)
    # A second drain at the same time skips it: cadence-spaced, not back-to-back.
    attempted = []
    result = outbox.drain(lambda item: attempted.append(item["id"]) or True, now=T0)
    assert attempted == [] and result["sent"] == 0 and result["remaining"] == 1
    # At the rescheduled time it is attempted again.
    result = outbox.drain(lambda item: True, now=T0 + timedelta(seconds=60))
    assert result["sent"] == 1 and result["remaining"] == 0


def test_sent_item_removed_and_never_resent(tmp_path):
    outbox = Outbox(tmp_path)
    item0 = outbox.queue("a@example.com", "one", "body")
    _set_next_attempt_at(outbox, item0["id"], T0)
    result = outbox.drain(lambda item: True, now=T0)
    assert result == {"sent": 1, "failed": 0, "remaining": 0}
    calls = []
    result = outbox.drain(lambda item: calls.append(item["id"]) or True, now=T0)
    assert calls == [] and result["sent"] == 0
    # An item already marked sent in the file is also never sent twice.
    outbox._save([{"id": "9", "to": "c@example.com", "subject": "old",
                   "body": "body", "status": "sent", "attempts": 1,
                   "next_attempt_at": T0.isoformat()}])
    result = outbox.drain(lambda item: calls.append(item["id"]) or True, now=T0)
    assert calls == [] and result["sent"] == 0


def test_sender_can_enqueue_during_drain(tmp_path):
    outbox = Outbox(tmp_path)
    original = outbox.queue("a@example.com", "original", "body")
    _set_next_attempt_at(outbox, original["id"], T0)
    enqueued = []

    def sender(item):
        assert item["idempotency_key"] == original["id"]
        enqueued.append(outbox.queue("b@example.com", "new", "body"))
        return True

    result = outbox.drain(sender, now=T0)
    assert result == {"sent": 1, "failed": 0, "remaining": 1}
    assert outbox.list() == enqueued


def test_tick_drains_outbox(tmp_path):
    outbox = Outbox(tmp_path)
    outbox.queue("a@example.com", "notice", "body")
    order = []
    loop = SchedulerLoop(_FakeScheduler([{"goal": "do the thing"}]),
                         _RecordingDispatcher(order),
                         outbox=outbox, outbox_sender=lambda item: True)
    due = loop.run_once()
    assert due == [{"goal": "do the thing"}]
    assert order == [("dispatch", "do the thing")]
    assert loop.last_outbox_drain == {"sent": 1, "failed": 0, "remaining": 0}
    assert outbox.list() == []


def test_on_due_called_before_dispatch(tmp_path):
    order = []
    loop = SchedulerLoop(
        _FakeScheduler([{"goal": "job one"}, {"goal": "job two"}]),
        _RecordingDispatcher(order),
        on_due=lambda event: order.append(("notify", event["goal"])))
    loop.run_once()
    assert order == [("notify", "job one"), ("dispatch", "job one"),
                     ("notify", "job two"), ("dispatch", "job two")]


def test_schedule_inspection_then_dispatch_does_not_consume_occurrence(tmp_path):
    from src.interface.triggers import Scheduler, SchedulerJob

    now = datetime(2026, 10, 31, 10, 0)
    scheduler = Scheduler(tmp_path)
    scheduler.add_job(SchedulerJob(name="daily", schedule="daily 09:00",
                                   goal="do the thing"))

    # Inspection must be read-only; the execution path still needs to claim
    # and dispatch that same occurrence.
    inspected = scheduler.due_jobs(now)
    assert [job.name for job in inspected] == ["daily"]
    assert scheduler.jobs[0].pending_run == ""

    order = []

    class _Dispatcher:
        def dispatch(self, event):
            order.append(("dispatch", event.goal))

    loop = SchedulerLoop(scheduler, _Dispatcher())
    assert [job.name for job in loop.run_once(now)] == ["daily"]
    assert order == [("dispatch", "do the thing")]
    assert Scheduler(tmp_path).claim_due(now) == []


def test_loop_without_outbox_still_runs(tmp_path):
    loop = SchedulerLoop(_FakeScheduler([{"goal": "x"}]), _RecordingDispatcher([]))
    assert loop.run_once() == [{"goal": "x"}]
    assert loop.last_outbox_drain is None


def test_scheduler_loop_runs_initial_tick_before_waiting(tmp_path):
    from src.interface.triggers import Scheduler, SchedulerJob

    scheduler = Scheduler(tmp_path)
    scheduler.add_job(SchedulerJob(name="daily", schedule="daily 09:00",
                                   goal="startup goal"))
    order = []
    loop = None

    class _StopAfterFirstDispatch:
        def dispatch(self, event):
            order.append(event.goal)
            loop.stop()

    loop = SchedulerLoop(scheduler, _StopAfterFirstDispatch(),
                         interval_seconds=60)
    loop.run()
    assert order == ["startup goal"]
    assert Scheduler(tmp_path).claim_due(datetime.now()) == []


def test_concurrent_processes_queue_without_lost_updates(tmp_path):
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_queue_many, args=(tmp_path, 15))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    items = Outbox(tmp_path).list()
    ids = [item["id"] for item in items]
    assert len(items) == 30
    assert len(ids) == len(set(ids))
    assert all(item["idempotency_key"] == item["id"] for item in items)


def test_concurrent_process_drains_send_an_item_once(tmp_path):
    context = multiprocessing.get_context("spawn")
    outbox = Outbox(tmp_path)
    queued = outbox.queue("a@example.com", "once", "body")
    barrier = context.Barrier(2)
    sent_ids = context.Queue()
    workers = [context.Process(target=_drain_after_barrier,
                               args=(tmp_path, barrier, sent_ids))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)
        assert worker.exitcode == 0

    sent = [sent_ids.get(timeout=1)]
    try:
        sent.append(sent_ids.get(timeout=0.1))
    except queue.Empty:
        pass
    assert sent == [queued["id"]]
    assert outbox.list() == []
