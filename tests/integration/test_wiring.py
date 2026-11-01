"""Wiring tests: Gmail, MCP client, pre-approval, memory extras, research, transcript, factory, polling."""

import json
import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.capabilities.gmail import gmail_sender, refresh_access_token
from src.interface.mcp_client import McpClient
from src.memory.memory_extras import backup, export_points, redact, rolling_summary
from src.interface.preapproval import PreapprovalStore
from src.agentic.research_allow import is_allowed, load_allowlist, research_multiround
from src.social.youtube_transcript import fetch_transcript
from src.interface.messaging import poll_slack_once, MessagingConfig, send_slack
from src.interface.webui import WebUIServer
from src.interface.triggers import TriggerDispatcher
from src.interface.triggers import TriggerEvent
from src.interface.slack_polling import (
    dispatch_slack_message, process_polled_messages)


def test_gmail_sender_fake(tmp_path):
    def fake_post(url, payload):
        return {"access_token": "tok123"}
    def fake_send(token, payload):
        assert token == "tok123"
        return {"id": "msg1"}
    sender = gmail_sender("cid", "csec", "r", post=fake_post, send=fake_send)
    assert sender("a@example.com", "Hi", "Body") == {"id": "msg1"}


def test_preapproval_refuses_external_and_irreversible(tmp_path):
    store = PreapprovalStore(tmp_path)
    assert store.grant("spec1", "send_external", 9999999999) is False
    assert store.grant("spec1", "delete", 9999999999) is False
    assert store.grant("spec1", "sandbox", 9999999999) is True
    assert store.is_preapproved("spec1", "sandbox", now=1000) is True
    assert store.is_preapproved("spec1", "send_external", now=1000) is False


def test_redact_and_rolling_summary(tmp_path):
    home = tmp_path
    path = rolling_summary(home, "dinner", "Email me at a@example.com about dinner", ["menu.pdf"], "done")
    assert "a@example.com" not in path.read_text()
    assert "menu.pdf" in path.read_text()


def test_backup_excludes_secrets(tmp_path):
    home = tmp_path / "home"
    (home / "specs").mkdir(parents=True)
    (home / "specs" / "a.md").write_text("spec")
    (home / ".env").write_text("AIDON_API_KEY=secret")
    copied = backup(home, tmp_path / "backup")
    assert "specs/a.md" in copied
    assert not (tmp_path / "backup" / ".env").exists()


def test_research_allowlist_and_multiround():
    assert is_allowed("https://www.save-on-foods.com/x", ["save-on-foods.com"]) is True
    assert is_allowed("https://other.com/x", ["save-on-foods.com"]) is False
    assert is_allowed("https://other.com/x", []) is True
    class R:
        def __init__(self):
            self.calls = 0
        def __call__(self, query):
            self.calls += 1
            return {"facts": []} if self.calls == 1 else {"facts": [{"url": "https://a.com"}]}
    researcher = R()
    result = research_multiround("q", researcher, rounds=2)
    assert researcher.calls == 2
    assert result["facts"]


def test_youtube_transcript_fake():
    xml = '<transcript><text start="0">Hello</text><text start="1">world</text></transcript>'
    assert fetch_transcript("vid", fetch=lambda url: xml) == "Hello world"
    assert fetch_transcript("vid", fetch=lambda url: "") == ""


def test_slack_polling_skips_bots_and_old():
    config = MessagingConfig(slack_bot_token="x")
    def fake_fetch(url, token, params):
        return {"messages": [{"ts": "4", "text": "bot", "bot_id": "B1"},
                              {"ts": "3", "text": "new"},
                              {"ts": "2", "text": "bot", "bot_id": "B1"},
                              {"ts": "1", "text": "old"}]}
    messages, newest = poll_slack_once(config, "C1", last_ts="1", fetch=fake_fetch)
    assert [m["text"] for m in messages] == ["new"]
    assert newest == "4"


def test_slack_polling_surfaces_api_permission_errors():
    config = MessagingConfig(slack_bot_token="x")

    with pytest.raises(RuntimeError, match="conversations.history failed: missing_scope"):
        poll_slack_once(
            config, "D1",
            fetch=lambda *_args: {"ok": False, "error": "missing_scope"})


def test_slack_http_error_logs_status_and_safe_api_code():
    config = MessagingConfig(slack_bot_token="x")

    def fail_post(*_args):
        raise urllib.error.HTTPError(
            "https://slack.com/api/chat.postMessage", 403, "Forbidden", {},
            io.BytesIO(b'{"error":"missing_scope","token":"secret"}'))

    result = send_slack(config, "C1", "hello", post=fail_post)
    assert result == {"ok": False, "error": "HTTP 403: missing_scope"}
    assert "secret" not in result["error"]


def test_slack_bot_replies_ignore_legacy_webhooks_and_preserve_thread():
    config = MessagingConfig(
        slack_bot_token="bot-token",
        slack_webhooks={"C1": "https://hooks.slack.com/services/revoked"})
    calls = []

    def post(url, token, payload):
        calls.append((url, token, payload))
        return {"ok": True, "ts": "2.0"}

    assert send_slack(config, "C1", "reply", thread_ts="1.0", post=post)["ok"]
    assert calls == [("https://slack.com/api/chat.postMessage", "bot-token",
                      {"channel": "C1", "text": "reply", "thread_ts": "1.0"})]


def test_slack_webhook_accepts_plain_ok_without_a_bot(monkeypatch):
    config = MessagingConfig(slack_webhooks={"C1": "https://hooks.slack.com/services/test"})
    requests = []

    def open_request(request, **kwargs):
        requests.append(request)
        return io.BytesIO(b"ok")

    monkeypatch.setattr("urllib.request.urlopen", open_request)
    assert send_slack(config, "C1", "reply") == {"ok": True, "webhook": True}
    assert "Authorization" not in requests[0].headers
    assert json.loads(requests[0].data) == {"text": "reply"}


def test_slack_poll_advances_cursor_only_after_handling_each_message():
    cursor = {"C1": "1"}
    handled = []

    def handle(message):
        assert cursor["C1"] < message["ts"]
        handled.append(message["ts"])

    def checkpoint(timestamp):
        assert handled[-1] == timestamp
        cursor["C1"] = timestamp

    process_polled_messages(
        [{"ts": "2"}, {"ts": "3"}], handle, checkpoint)
    assert cursor["C1"] == "3"


def test_slack_dispatch_replies_when_handler_raises():
    dispatcher = TriggerDispatcher()

    def fail(_event):
        raise RuntimeError("private detail must not reach Slack")

    dispatcher.register("slack", fail)
    event = TriggerEvent(source="slack", goal="hello")
    sent = []
    dispatch_slack_message(
        dispatcher, event, channel="C1", thread_ts="123.4",
        sender=lambda channel, text, thread_ts=None:
            sent.append((channel, text, thread_ts)), ack_after=1)
    assert len(sent) == 1
    assert sent[0][0] == "C1"
    assert sent[0][2] == "123.4"
    assert "internal error" in sent[0][1]
    assert "private detail" not in sent[0][1]


def test_factory_stages_from_trace(tmp_path):
    home = tmp_path
    (home / "trace").mkdir()
    (home / "trace" / "run1.jsonl").write_text(json.dumps({"kind": "plan", "status": "done"}) + "\n")
    server = WebUIServer(home, dispatcher=TriggerDispatcher())
    stages = server.factory_stages()
    assert stages and stages[0]["kind"] == "plan"


def test_mcp_client_fake_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    method = req.get('method')\n"
        "    result = {'tools': [{'name': 'echo'}]} if method == 'tools/list' else {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id'), 'result': result}), flush=True)\n")
    client = McpClient(["python3", str(script)])
    client.start()
    assert client.list_tools() == [{"name": "echo"}]
    client.close()


def test_slack_history_pagination_keeps_all_messages_and_attachments():
    config = MessagingConfig(slack_bot_token="x")
    calls = []

    def fetch(url, token, params):
        calls.append(params)
        if params.get("cursor") == "next":
            return {"ok": True, "messages": [{"ts": "9.0", "text": "older"}]}
        return {"ok": True, "messages": [
            {"ts": "11.0", "text": "newest"},
            {"ts": "10.0", "text": "file", "files": [{"id": "F1"}]}],
            "response_metadata": {"next_cursor": "next"}}

    messages, newest = poll_slack_once(config, "C1", last_ts="8.0", fetch=fetch)
    assert [m["ts"] for m in messages] == ["9.0", "10.0", "11.0"]
    assert messages[1]["files"] == [{"id": "F1"}]
    assert newest == "11.0"
    assert calls[1]["oldest"] == "8.0"


def test_slack_history_rate_limit_preserves_retry_delay(monkeypatch):
    from src.interface.messaging import SlackRateLimited

    def limited(*args, **kwargs):
        raise urllib.error.HTTPError("https://slack.com/api/conversations.history",
                                     429, "Too Many Requests", {"Retry-After": "90"}, io.BytesIO(b""))

    monkeypatch.setattr("urllib.request.urlopen", limited)
    with pytest.raises(SlackRateLimited) as error:
        poll_slack_once(MessagingConfig(slack_bot_token="x"), "C1")
    assert error.value.retry_after == 90
