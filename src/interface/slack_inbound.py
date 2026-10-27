"""Slack inbound, old Aidon slack_inbound concept, rebuilt as a separate module.

Slack inbound is signature verification plus event parsing, nothing else: verify_slack_event
checks the HMAC signature and the 5-minute timestamp, parse_slack_event returns a
TriggerEvent for a message event, or None for other events, so the dispatcher never sees
an unverified or non-message event. Sending is in messaging.send_slack, this module is
inbound only, on purpose.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from .triggers import TriggerEvent


def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > 300:
        return False
    base = f"v0:{timestamp}:".encode("utf-8") + body
    expected = "v0=" + hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def parse_slack_event(payload: dict, project_id: str = "default") -> TriggerEvent | None:
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    if event.get("type") != "message" or event.get("bot_id"):
        return None
    text = event.get("text", "")
    if not text:
        return None
    return TriggerEvent(source="slack", project_id=project_id, goal=text, payload={"channel": event.get("channel", ""), "user": event.get("user", "")}, reply=lambda reply: None)
