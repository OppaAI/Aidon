"""Messaging retries, durable receipt, and approvals during active goals."""

from datetime import datetime, timedelta, timezone
import threading

import pytest

from src.interface.reply_delivery import ReplySender
from src.interface.slack_inbox import SlackInbox
from src.interface.slack_polling import process_polled_messages
from src.interface.triggers import TriggerDispatcher, TriggerEvent
from src.interface.webui import WebUIServer


def test_failed_reply_survives_restart_and_retry_does_not_rerun_goal(tmp_path):
    attempts = []

    def fail(channel, text, thread):
        attempts.append((channel, text, thread))
        return {"ok": False, "error": "HTTP 404"}

    sender = ReplySender(tmp_path, "slack", fail)
    assert sender("C1", "finished", "1.0")["queued"]
    assert len(sender.outbox.list()) == 1
    delivered = []
    restarted = ReplySender(tmp_path, "slack", lambda *args: delivered.append(args) or {"ok": True})
    restarted.retry_due(now=datetime.now(timezone.utc) + timedelta(minutes=2))
    assert delivered == [("C1", "finished", "1.0")]
    assert not restarted.outbox.list()
    assert attempts == [("C1", "finished", "1.0")]


def test_slack_cursor_is_not_acknowledged_when_queueing_fails():
    checkpoints = []
    def fail(_message):
        raise OSError("disk full")
    with pytest.raises(OSError):
        process_polled_messages([{"ts": "1.0"}], fail, checkpoints.append)
    assert checkpoints == []


def test_slack_inbox_persists_and_deduplicates_pending_jobs(tmp_path):
    dispatcher = TriggerDispatcher()
    goals, sent = [], []
    dispatcher.register("slack", lambda event: goals.append(event.goal) or event.send_reply("done"))
    send = lambda *args: sent.append(args) or {"ok": True}
    event = TriggerEvent(source="slack", conversation_id="slack:C1", goal="hello",
                         payload={"channel": "C1", "slack_ts": "2.0", "thread_ts": "1.0"})
    first = SlackInbox(tmp_path, dispatcher, send)
    first.enqueue(event)
    first.enqueue(event)
    restarted = SlackInbox(tmp_path, dispatcher, send)
    restarted.recover()
    assert restarted.process_once()
    restarted.enqueue(event)
    assert not restarted.process_once()
    assert goals == ["hello"]
    assert sent == [("C1", "done", "1.0")]


def test_slack_inbox_does_not_reexecute_interrupted_work_without_owner(tmp_path):
    dispatcher = TriggerDispatcher()
    goals, sent = [], []
    dispatcher.register("slack", lambda event: goals.append(event.goal))
    inbox = SlackInbox(tmp_path, dispatcher, lambda *args: sent.append(args))
    inbox.enqueue(TriggerEvent(source="slack", goal="side effect",
                               payload={"channel": "C1", "slack_ts": "1.0"}))
    with inbox._connect() as db:
        db.execute("UPDATE messages SET status='running'")
    inbox.recover()
    assert not inbox.process_once()
    assert not goals
    assert "Review the run" in sent[0][1]
    with inbox._connect() as db:
        assert db.execute("SELECT status FROM messages").fetchone()[0] == "interrupted"


def test_corrupt_reply_outbox_is_preserved_instead_of_overwritten(tmp_path):
    sender = ReplySender(tmp_path, "slack", lambda *args: {"ok": True})
    sender.outbox.path.write_text("{corrupt")
    with pytest.raises(ValueError, match="corrupt"):
        sender("C1", "reply")
    assert sender.outbox.path.read_text() == "{corrupt"


def test_slack_poll_commands_can_approve_a_goal_while_worker_waits(tmp_path):
    server = WebUIServer(tmp_path)
    server.webui_gate.timeout_seconds = 3
    server.webui_gate.slack_sender = lambda *args: sent.append(args) or {"ok": True}
    server.slack_sender = server.webui_gate.slack_sender
    sent, ready, verdicts = [], threading.Event(), []

    def goal(event):
        ready.set()
        verdicts.append(server.webui_gate.gate.ask(
            "live", "Test", "Harmless test", context={"slack_channel": "C1"}).verdict)
        event.send_reply("approved")

    server.dispatcher.register("slack", goal)
    inbox = SlackInbox(tmp_path, server.dispatcher, server.slack_sender)
    status, result = server.handle_slack_message(
        {"event": {"type": "message", "channel": "D1", "ts": "1.0", "text": "run"}},
        dispatch=inbox.enqueue)
    assert result == {"queued": True}
    worker = threading.Thread(target=inbox.process_once)
    worker.start()
    assert ready.wait(1)
    # Wait for the actual approval card rather than assuming worker timing.
    for _ in range(100):
        if sent:
            break
        threading.Event().wait(.01)
    card_id = next(iter(server.webui_gate.cards))
    assert f"Approval needed [{card_id}]" in sent[0][1]
    status, result = server.handle_slack_message(
        {"event": {"type": "message", "channel": "D1", "ts": "2.0", "text": f"approve {card_id}"}},
        dispatch=inbox.enqueue)
    worker.join(3)
    assert not worker.is_alive()
    assert verdicts == ["approved"]
    assert result["replies"] == [f"Approved {card_id}"]


def test_polled_slack_uses_shared_commands_and_conversation_identity(tmp_path):
    server = WebUIServer(tmp_path)
    events = []
    status, result = server.handle_slack_message(
        {"event": {"type": "message", "channel": "D1", "ts": "1.0", "text": "/help"}},
        dispatch=events.append)
    assert result["replies"] and not events
    server.handle_slack_message(
        {"event": {"type": "message", "channel": "D1", "ts": "2.0", "text": "hello"}},
        dispatch=events.append)
    assert events[0].conversation_id == "slack:D1"
    assert events[0].payload["conversation_id"] == "slack:D1"


def test_new_slack_channel_does_not_replay_historical_goals(tmp_path):
    from src.interface.slack_polling import load_poll_cursors
    path = tmp_path / "cursors.json"
    assert load_poll_cursors(path, ["C1"], now=100) == {"C1": "100"}
    assert load_poll_cursors(path, ["C1", "D1"], now=200) == {"C1": "100", "D1": "200"}


def test_corrupt_slack_cursor_does_not_reset_into_a_full_replay(tmp_path):
    from src.interface.slack_polling import load_poll_cursors
    path = tmp_path / "cursors.json"
    path.write_text("{corrupt")
    with pytest.raises(ValueError):
        load_poll_cursors(path, ["C1"], now=100)
    assert path.read_text() == "{corrupt"


def test_signed_slack_webhook_queues_once_and_returns_before_goal_runs(tmp_path):
    import hashlib
    import hmac
    import json
    import time
    server = WebUIServer(tmp_path)
    server.slack_signing_secret = "secret"
    goals = []
    server.dispatcher.register("slack", lambda event: goals.append(event.goal))
    inbox = SlackInbox(tmp_path, server.dispatcher, lambda *args: {"ok": True})
    server.slack_message_dispatch = inbox.enqueue
    body = {"event": {"type": "message", "channel": "D1", "ts": "1.0", "text": "run"}}
    raw = json.dumps(body).encode()
    stamp = str(int(time.time()))
    signature = "v0=" + hmac.new(b"secret", f"v0:{stamp}:".encode()+raw, hashlib.sha256).hexdigest()
    headers = {"x-slack-request-timestamp": stamp, "x-slack-signature": signature}
    assert server.handle_slack_post(raw, headers, body) == (200, {"queued": True})
    assert server.handle_slack_post(raw, headers, body) == (200, {"queued": True})
    assert goals == []
    assert inbox.process_once()
    assert goals == ["run"]
    assert not inbox.process_once()
