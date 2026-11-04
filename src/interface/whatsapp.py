"""WhatsApp, Aidon whatsapp.py concept, rebuilt as a separate module.

WhatsApp is signature verification, message parsing, and sending, separate from Slack:
verify_whatsapp_signature checks the app-secret signature, parse_whatsapp_message returns
a TriggerEvent for a text or validated location message, or None for other types, and send_whatsapp_message
sends via the Cloud API, or returns an error if no token is configured, never a fake send.
Messaging.py still holds the shared config and senders, this module is the WhatsApp-only
surface, so a WhatsApp change does not touch Slack code.
"""

from __future__ import annotations

import math

from .messaging import MessagingConfig, send_whatsapp
from .triggers import TriggerEvent


def parse_whatsapp_location(message: dict) -> dict | None:
    """Return validated coordinates and optional label from a location message."""
    if not isinstance(message, dict) or message.get("type") != "location":
        return None
    raw = message.get("location")
    if not isinstance(raw, dict):
        return None
    try:
        latitude = float(raw.get("latitude"))
        longitude = float(raw.get("longitude"))
    except (TypeError, ValueError):
        return None
    if (not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180):
        return None
    return {
        "latitude": latitude,
        "longitude": longitude,
        "name": str(raw.get("name", "") or "")[:200],
        "address": str(raw.get("address", "") or "")[:500],
    }


def whatsapp_location_context(location: dict) -> str:
    """Turn an explicitly shared pin into clear context for the goal runner."""
    label = "; ".join(x for x in (location.get("name"),
                                   location.get("address")) if x)
    suffix = f" Place label: {label}." if label else ""
    return (
        "I shared my current location in WhatsApp: latitude "
        f"{location['latitude']:.6f}, longitude "
        f"{location['longitude']:.6f}.{suffix} Use it as my starting point "
        "for my nearby-place request. If I have not said what I am looking "
        "for, ask me."
    )


def parse_whatsapp_message(payload: dict) -> TriggerEvent | None:
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        message = entry["messages"][0]
    except (KeyError, IndexError, TypeError):
        return None
    location = parse_whatsapp_location(message)
    if location is not None:
        return TriggerEvent(
            source="whatsapp",
            conversation_id="whatsapp:" + str(message.get("from", "")),
            goal=whatsapp_location_context(location),
            payload={"from": message.get("from", ""), "location": location},
            reply=lambda reply: None)
    if message.get("type") != "text":
        return None
    text = message.get("text", {}).get("body", "")
    if not text:
        return None
    return TriggerEvent(source="whatsapp", conversation_id="whatsapp:" + str(message.get("from", "")), goal=text, payload={"from": message.get("from", "")}, reply=lambda reply: None)


def send_whatsapp_message(config: MessagingConfig, to: str, text: str, post=None) -> dict:
    if not config.whatsapp_configured():
        return {"ok": False, "error": "WhatsApp not configured, missing token, phone id, or owner number, message not sent"}
    result = send_whatsapp(config, to, text, post=post)
    return {"ok": True, "result": result}
