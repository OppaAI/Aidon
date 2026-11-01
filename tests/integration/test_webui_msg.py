"""WebUI server, WhatsApp and Slack two-way, no Telegram.

Server is tested on localhost, with fake reply callbacks,
no real WhatsApp or Slack API call, no real account.
Slack signature is verified before any event is parsed.
"""

import hashlib
import hmac
import json
import threading
import time
import urllib.request
from http.server import HTTPServer

from src.interface.messaging import (MessagingConfig, normalize_number, parse_decision,
                           send_slack, send_whatsapp,
                           verify_whatsapp_signature)
from src.interface.triggers import TriggerDispatcher
from src.interface.v1 import AuthStore
from src.interface.webui import WebUIServer


def _start_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "library" / "files").mkdir(parents=True, exist_ok=True)
    (home / "library" / "files" / "note.txt").write_text("hello", encoding="utf-8")
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", lambda e: e.send_reply(f"WebUI got: {e.goal}"))
    dispatcher.register("whatsapp", lambda e: e.send_reply(f"WhatsApp got: {e.goal}"))
    dispatcher.register("slack", lambda e: e.send_reply(f"Slack got: {e.goal}"))
    whatsapp_replies: list = []
    slack_replies: list = []
    auth = AuthStore(home)
    key, _ = auth.create_key("owner", "owner")
    msg_config = MessagingConfig(
        whatsapp_token="fake-wa", whatsapp_phone_id="12345",
        whatsapp_verify_token="verify123", whatsapp_app_secret="",
        whatsapp_owner="+16045551234",
        slack_bot_token="xoxb-fake", slack_signing_secret="secret123")
    server = WebUIServer(home, dispatcher=dispatcher, auth_store=auth,
                         messaging_config=msg_config,
                         whatsapp_sender=lambda to, text: whatsapp_replies.append((to, text)),
                         slack_sender=lambda channel, text, thread_ts=None: slack_replies.append((channel, text, thread_ts)))
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    return server, httpd, base, key, whatsapp_replies, slack_replies


def _post(base, path, payload, headers=None):
    data = json.dumps(payload).encode()
    request = urllib.request.Request(base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request) as response:
        return response.status, json.loads(response.read())


def _get(base, path):
    import urllib.error
    try:
        with urllib.request.urlopen(base + path) as response:
            text = response.read().decode()
            try:
                return response.status, json.loads(text)
            except json.JSONDecodeError:
                return response.status, text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


def test_webui_chat_library_settings_no_real_key(tmp_path):
    server, httpd, base, key, _, _ = _start_server(tmp_path)
    try:
        status, html = _get(base, "/")
        assert status == 200 and "Aidon" in html
        status, payload = _post(base, "/api/chat", {"message": "Plan a dinner"})
        assert payload["reply"] == "WebUI got: Plan a dinner"
        status, payload = _get(base, "/api/library")
        assert any(f["name"] == "note.txt" for f in payload["files"])
        status, html = _get(base, "/settings")
        assert status == 200 and "settings-groups" in html
    finally:
        httpd.shutdown()


def test_run_snapshot_returns_only_unseen_events(tmp_path):
    server = WebUIServer(tmp_path)
    server._web_runs["run-1"] = {
        "run_id": "run-1", "status": "running",
        "events": [{"kind": "aidon", "text": "one"},
                    {"kind": "aidon", "text": "two"}],
        "trace": [{"phase": "interpret"}, {"phase": "respond"}],
    }
    snapshot = server.run_snapshot("run-1", since=1, trace_since=1)
    assert snapshot["event_total"] == 2
    assert [event["text"] for event in snapshot["events"]] == ["two"]
    assert snapshot["trace_total"] == 2
    assert len(snapshot["trace"]) == 1


def test_whatsapp_verify_and_message_two_way(tmp_path):
    server, httpd, base, _, replies, _ = _start_server(tmp_path)
    try:
        status, text = _get(base, "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=verify123&hub.challenge=abc")
        assert status == 200 and text == "abc"
        status, text = _get(base, "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc")
        assert status == 403
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "+16045551234", "text": {"body": "Hello Aidon"}}]}}]}]}
        status, result = _post(base, "/webhook/whatsapp", payload)
        assert result["replies"] == ["WhatsApp got: Hello Aidon"]
        assert replies == [("+16045551234", "WhatsApp got: Hello Aidon")]
        # Non-owner is ignored, owner only, fail closed.
        stranger = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "+19998887777", "text": {"body": "Hello Aidon"}}]}}]}]}
        status, result = _post(base, "/webhook/whatsapp", stranger)
        assert result["replies"] == []
        assert len(replies) == 1
    finally:
        httpd.shutdown()


def test_messaging_old_keys_senders_and_decisions():
    config = MessagingConfig.from_env({
        "AIDON_WHATSAPP_TOKEN": "wa", "AIDON_WHATSAPP_PHONE_ID": "pid",
        "AIDON_WHATSAPP_VERIFY_TOKEN": "vt", "AIDON_WHATSAPP_APP_SECRET": "sec",
        "AIDON_WHATSAPP_OWNER": "+1 (604) 555-1234",
        "SLACK_BOT_TOKEN": "xoxb-1", "AIDON_SLACK_SIGNING_SECRET": "sig"})
    assert config.whatsapp_owner == "+1 (604) 555-1234"
    assert normalize_number(config.whatsapp_owner) == "16045551234"
    assert parse_decision("approve card-1") == ("approve", "card-1")
    assert parse_decision("Approve") == ("approve", None)
    assert parse_decision("Hello") is None
    calls = []

    def fake_post(url, token, payload):
        calls.append((url, token, payload))
        if "slack.com" in url:
            return {"ok": True, "ts": "123.456"}
        return {"messages": [{"id": "wamid.1"}]}

    assert send_whatsapp(config, "+16045551234", "hi", post=fake_post)["ok"] is True
    assert send_slack(config, "C1", "hi", thread_ts="1.0", post=fake_post)["ok"] is True
    assert calls[0][0].endswith("/pid/messages")
    assert calls[1][2]["thread_ts"] == "1.0"
    raw = b"hello"
    sig = "sha256=" + hmac.new(b"sec", raw, hashlib.sha256).hexdigest()
    assert verify_whatsapp_signature(raw, "sec", sig) is True
    assert verify_whatsapp_signature(raw, "sec", "sha256=bad") is False


def test_whatsapp_approve_resolves_card(tmp_path):
    server, httpd, base, _, replies, _ = _start_server(tmp_path)
    try:
        # Create a card in a thread, then approve it from WhatsApp.
        card_id_holder: list[str] = []

        def decide(kind, title, body):
            from src.interface.approval import Decision
            card_id_holder.append("card-1")
            return Decision("approved")

        server.webui_gate.cards["card-1"] = {"kind": "plan", "title": "t", "body": "b", "verdict": None}
        import threading as _threading
        server.webui_gate._events["card-1"] = _threading.Event()
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "+16045551234", "text": {"body": "approve card-1"}}]}}]}]}
        status, result = _post(base, "/webhook/whatsapp", payload)
        assert result["replies"] == ["Approved card-1"]
        assert replies == [("+16045551234", "Approved card-1")]
    finally:
        httpd.shutdown()


def test_bare_approve_resolves_the_only_pending_card(tmp_path):
    import threading

    server = WebUIServer(tmp_path)
    card_id = "card-only"
    event = threading.Event()
    server.webui_gate.cards[card_id] = {
        "kind": "install", "title": "Create Python virtual environment",
        "body": "Create the isolated environment", "verdict": None,
        "edited_text": "", "context": {},
    }
    server.webui_gate._events[card_id] = event

    assert server._resolve_decision("Approve") == "Approved card-only"
    assert event.is_set()
    assert server.webui_gate.cards[card_id]["verdict"] == "approved"


def test_bare_approve_does_not_guess_when_multiple_cards_are_pending(tmp_path):
    import threading

    server = WebUIServer(tmp_path)
    for card_id, title in (("card-a", "Install package"),
                           ("card-b", "Publish post")):
        server.webui_gate.cards[card_id] = {
            "kind": "live", "title": title, "body": "", "verdict": None,
            "edited_text": "", "context": {},
        }
        server.webui_gate._events[card_id] = threading.Event()

    reply = server._resolve_decision("approve")
    assert "2 approvals pending" in reply
    assert "Install package" in reply and "Publish post" in reply
    assert all(card["verdict"] is None
               for card in server.webui_gate.cards.values())


def test_slack_signature_verified_before_parse(tmp_path):
    server, httpd, base, _, _, replies = _start_server(tmp_path)
    try:
        payload = {"event": {"type": "message", "text": "Hello Slack", "channel": "D1", "ts": "10.0"}}
        raw = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"secret123", f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
        status, result = _post(base, "/webhook/slack", payload,
                               headers={"X-Slack-Request-Timestamp": timestamp,
                                        "X-Slack-Signature": signature})
        assert status == 200
        assert result["replies"] == ["Slack got: Hello Slack"]
        assert replies == [("D1", "Slack got: Hello Slack", "10.0")]
        # Bad signature is 401, never dispatched.
        status = None
        try:
            _post(base, "/webhook/slack", payload,
                  headers={"X-Slack-Request-Timestamp": timestamp,
                           "X-Slack-Signature": "v0=bad"})
        except Exception as exc:
            status = exc.code
        assert status == 401
        # Bot messages are ignored, not treated as goals.
        bot_payload = {"event": {"type": "message", "bot_id": "B1", "text": "bot"}}
        raw2 = json.dumps(bot_payload).encode()
        sig2 = "v0=" + hmac.new(b"secret123", f"v0:{timestamp}:".encode() + raw2, hashlib.sha256).hexdigest()
        status, result = _post(base, "/webhook/slack", bot_payload,
                               headers={"X-Slack-Request-Timestamp": timestamp,
                                        "X-Slack-Signature": sig2})
        assert result.get("ok") is True
    finally:
        httpd.shutdown()


def test_slack_rejected_without_signing_secret(tmp_path):
    server, httpd, base, _, _, replies = _start_server(tmp_path)
    try:
        server.slack_signing_secret = ""
        payload = {"event": {"type": "message", "text": "Hello Slack",
                             "channel": "C1", "ts": "10.0"}}
        status, _ = server.handle_slack_post(
            json.dumps(payload).encode(), {}, payload)
        assert status == 401
        assert replies == []
    finally:
        httpd.shutdown()
