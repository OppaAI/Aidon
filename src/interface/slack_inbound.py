"""Slack inbound, old Aidon slack_inbound concept, rebuilt as a separate module.

Slack inbound is signature verification plus event parsing, nothing else: verify_slack_event
checks the HMAC signature and the 5-minute timestamp, parse_slack_event returns a
TriggerEvent for a message event, or None for other events, so the dispatcher never sees
an unverified or non-message event. Image file shares ride along like the WhatsApp path:
an [image ...] marker on the goal text plus the file metadata in the payload. Slack file
bytes are NOT downloaded here — file URLs need a bot token this path does not carry —
so an image-only message still becomes a goal, with the marker saying the bytes were
not fetched. Sending is in messaging.send_slack, this module is
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


def _image_files(event: dict) -> list[dict]:
    """Image file shares carried by the event payload, if any.

    Slack sends shared files under "files" (current) or "file_shares"
    (older events). Only image MIME types are kept. No download is
    attempted here: Slack file URLs need a bot token the inbound
    path does not carry, so the payload metadata is attached as-is
    and the marker says so — the bytes are not fetched, and no
    credential flow is invented.
    """
    seen: dict[str, dict] = {}
    for key in ("files", "file_shares"):
        for item in event.get(key, []) or []:
            if not isinstance(item, dict):
                continue
            mimetype = item.get("mimetype", "") or ""
            filetype = (item.get("filetype", "") or "").lower()
            is_image = mimetype.startswith("image/") or filetype in (
                "jpg", "jpeg", "png", "gif", "webp")
            if not is_image:
                continue
            file_id = str(item.get("id", ""))
            seen[file_id or item.get("name", "")] = {
                "id": file_id,
                "name": item.get("name", ""),
                "mimetype": mimetype or f"image/{filetype}",
                "url_private": item.get("url_private", ""),
                "downloaded": False,
                "download_note": ("Slack file URLs need a bot token; "
                                  "the inbound path carries payload "
                                  "metadata only, not the file bytes."),
            }
    return list(seen.values())


def parse_slack_event(payload: dict, project_id: str = "default") -> TriggerEvent | None:
    event = payload.get("event", {}) if isinstance(payload, dict) else {}
    if event.get("type") != "message" or event.get("bot_id"):
        return None
    text = event.get("text", "")
    images = _image_files(event)
    if not text and not images:
        return None
    # Same downstream shape as the WhatsApp path: the image rides
    # with the goal text as an [image ...] marker, and the file
    # metadata rides in the event payload.
    for image in images:
        text = (text + f" [image slack_file_id={image['id']} "
                f"mimetype={image['mimetype']} name={image['name']}]").strip()
    return TriggerEvent(source="slack", project_id=project_id, goal=text, payload={"channel": event.get("channel", ""), "user": event.get("user", ""), "images": images}, reply=lambda reply: None)
