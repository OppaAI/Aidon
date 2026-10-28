"""Outbox cadence, tick draining, and scheduled-run notification."""

import json
from datetime import datetime, timedelta, timezone

from src.capabilities.outbox import Outbox
from src.interface.scheduler_loop import SchedulerLoop

T0 = datetime(2026, 10, 26, 12, 0, 0, tzinfo=timezone.utc)


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


def test_loop_without_outbox_still_runs(tmp_path):
    loop = SchedulerLoop(_FakeScheduler([{"goal": "x"}]), _RecordingDispatcher([]))
    assert loop.run_once() == [{"goal": "x"}]
    assert loop.last_outbox_drain is None
