"""WhatsApp, Aidon whatsapp.py concept, rebuilt as a separate module.

WhatsApp is signature verification, message parsing, and sending, separate from Slack:
verify_whatsapp_signature checks the app-secret signature, parse_whatsapp_message returns
a TriggerEvent for a text message, or None for other types, and send_whatsapp_message
sends via the Cloud API, or returns an error if no token is configured, never a fake send.
Messaging.py still holds the shared config and senders, this module is the WhatsApp-only
surface, so a WhatsApp change does not touch Slack code.
"""

from __future__ import annotations

from .messaging import MessagingConfig, send_whatsapp
from .triggers import TriggerEvent


def parse_whatsapp_message(payload: dict, project_id: str = "default") -> TriggerEvent | None:
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        message = entry["messages"][0]
    except (KeyError, IndexError, TypeError):
        return None
    if message.get("type") != "text":
        return None
    text = message.get("text", {}).get("body", "")
    if not text:
        return None
    return TriggerEvent(source="whatsapp", project_id=project_id, goal=text, payload={"from": message.get("from", "")}, reply=lambda reply: None)


def send_whatsapp_message(config: MessagingConfig, to: str, text: str, post=None) -> dict:
    if not config.whatsapp_configured():
        return {"ok": False, "error": "WhatsApp not configured, missing token, phone id, or owner number, message not sent"}
    result = send_whatsapp(config, to, text, post=post)
    return {"ok": True, "result": result}
