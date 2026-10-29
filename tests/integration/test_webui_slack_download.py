"""Slack image bytes flow into goals when the server has a bot token.

Regression: Slack file shares arrived as markers + metadata only, with
downloaded False, even when the server's messaging config carried a
bot token that could fetch them. The WhatsApp path already saved
bytes to the library with a vision marker; Slack now mirrors it.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import src.interface.messaging as messaging
from src.interface.messaging import MessagingConfig
from src.interface.webui import WebUIServer


def _sign(secret, raw):
    """Valid Slack HMAC headers for raw, as production sends them."""
    import hashlib
    import hmac
    import time

    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw,
                            hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def _slack_body():
    return {
        "event": {
            "type": "message",
            "text": "what is in this photo",
            "channel": "C1",
            "user": "U1",
            "ts": "123.456",
            "files": [{
                "id": "F1",
                "name": "photo.png",
                "mimetype": "image/png",
                "url_private": "https://files.slack.com/photo.png",
            }],
        },
    }


def test_slack_image_downloaded_when_token_present(monkeypatch):
    with TemporaryDirectory() as td:
        home = Path(td)
        cfg = MessagingConfig(slack_bot_token="xoxb-test", slack_signing_secret="secret123")
        srv = WebUIServer(home, None, None, messaging_config=cfg)
        captured = {}
        srv.dispatcher.handlers["slack"] = lambda event: captured.setdefault("event", event)
        monkeypatch.setattr(
            messaging, "download_slack_file",
            lambda token, url: b"fake-png-bytes")
        body = _slack_body()
        raw = json.dumps(body).encode()
        status, payload = srv.handle_slack_post(raw, _sign("secret123", raw), body)
        assert status == 200
        event = captured["event"]
        assert "library_id=" in event.goal
        assert "slack_file_id=" not in event.goal
        assert event.payload["images"][0]["downloaded"] is True


def test_slack_image_marker_only_without_token():
    with TemporaryDirectory() as td:
        home = Path(td)
        srv = WebUIServer(home, None, None)  # no bot token
        srv.slack_signing_secret = "secret123"
        captured = {}
        srv.dispatcher.handlers["slack"] = lambda event: captured.setdefault("event", event)
        body = _slack_body()
        raw = json.dumps(body).encode()
        status, _ = srv.handle_slack_post(raw, _sign("secret123", raw), body)
        assert status == 200
        event = captured["event"]
        assert "slack_file_id=F1" in event.goal
        assert event.payload["images"][0]["downloaded"] is False
