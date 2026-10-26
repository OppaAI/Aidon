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

from src.triggers import TriggerDispatcher
from src.v1 import AuthStore
from src.webui import WebUIServer


def _start_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "library" / "files").mkdir(parents=True, exist_ok=True)
    (home / "library" / "files" / "note.txt").write_text("hello", encoding="utf-8")
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", lambda e: e.send_reply(f"WebUI got: {e.goal}"))
    dispatcher.register("whatsapp", lambda e: e.send_reply(f"WhatsApp got: {e.goal}"))
    dispatcher.register("slack", lambda e: e.send_reply(f"Slack got: {e.goal}"))
    whatsapp_replies: list[str] = []
    slack_replies: list[str] = []
    auth = AuthStore(home)
    key, _ = auth.create_key("owner", "owner")
    server = WebUIServer(home, dispatcher=dispatcher, auth_store=auth,
                         whatsapp_reply=whatsapp_replies.append,
                         slack_reply=slack_replies.append,
                         whatsapp_verify_token="verify123",
                         slack_signing_secret="secret123")
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
        assert key not in html
        assert "hash" in html.lower() or "Keys" in html
    finally:
        httpd.shutdown()


def test_whatsapp_verify_and_message_two_way(tmp_path):
    server, httpd, base, _, replies, _ = _start_server(tmp_path)
    try:
        status, text = _get(base, "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=verify123&hub.challenge=abc")
        assert status == 200 and text == "abc"
        status, text = _get(base, "/webhook/whatsapp?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc")
        assert status == 403
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "123", "text": {"body": "Hello Aidon"}}]}}]}]}
        status, result = _post(base, "/webhook/whatsapp", payload)
        assert result["replies"] == ["WhatsApp got: Hello Aidon"]
        assert replies == ["WhatsApp got: Hello Aidon"]
    finally:
        httpd.shutdown()


def test_slack_signature_verified_before_parse(tmp_path):
    server, httpd, base, _, _, replies = _start_server(tmp_path)
    try:
        payload = {"event": {"type": "message", "text": "Hello Slack"}}
        raw = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        signature = "v0=" + hmac.new(b"secret123", f"v0:{timestamp}:".encode() + raw, hashlib.sha256).hexdigest()
        status, result = _post(base, "/webhook/slack", payload,
                               headers={"X-Slack-Request-Timestamp": timestamp,
                                        "X-Slack-Signature": signature})
        assert status == 200
        assert result["replies"] == ["Slack got: Hello Slack"]
        assert replies == ["Slack got: Hello Slack"]
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
