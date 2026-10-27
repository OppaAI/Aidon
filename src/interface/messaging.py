"""WhatsApp and Slack, two-way, following old Aidon, with old key names.

Old key names are canonical here, exactly as in old Aidon:
- WhatsApp: AIDON_WHATSAPP_TOKEN, AIDON_WHATSAPP_PHONE_ID,
  AIDON_WHATSAPP_VERIFY_TOKEN, AIDON_WHATSAPP_APP_SECRET,
  AIDON_WHATSAPP_OWNER
- Slack: SLACK_BOT_TOKEN, plus SLACK_SIGNING_SECRET, which is
  Slack's own name, not an old Aidon name, because old Slack
  was polling, not a webhook, so it had no signing secret.
  AIDON_SLACK_INBOUND_CHANNEL, AIDON_SLACK_POLL_SECONDS, and
  AIDON_SLACK_ACK_AFTER_SECONDS belong to that old polling
  listener, not to the webhook, and are not used here.

Two-way, both channels:
- WhatsApp: inbound webhook message from the owner only
  becomes a goal, replies go back over WhatsApp to that
  same sender number, with the WhatsApp Cloud API.
- Slack: inbound webhook message becomes a goal, replies
  go back over Slack to that same channel, in thread when
  the incoming message has a thread_ts, with
  chat.postMessage. Old Slack was polling DMs and channels,
  new Slack is a webhook, so the Event Subscriptions you
  choose in the Slack app decide the surface: subscribe to
  message.im for DMs, message.channels for channels.
  Bot messages are ignored, never goals, never replies.
- Approve and reject, both channels: a message
  "approve <id>" or "reject <id>" resolves a pending
  approval card, it does not become a goal. This is the
  old WhatsApp and Slack approval reply, carried over.

WhatsApp owner gate, fail closed:
Only AIDON_WHATSAPP_OWNER's number becomes a goal.
Numbers are normalized to digits only, and compared
exactly, after normalization. Empty owner means inbound
is disabled, not open to everyone. A non-owner message
is ignored, never dispatched, never replied to.
Slack has no owner gate in old Aidon, it has a bot
token and channel subscriptions, so no owner gate is
added for Slack here.

WhatsApp signature, fail closed when configured:
When AIDON_WHATSAPP_APP_SECRET is set, the
X-Hub-Signature-256 header must match HMAC SHA256 of
the raw body. A bad signature is rejected, never parsed.
When it is not set, the hub verify token is the only
check, which is weaker, and the server warns at startup.

Sending, stdlib only, no secrets in logs:
send_whatsapp and send_slack take a post function,
so tests inject a fake and never hit Meta or Slack.
In production the post function is urllib, with a
Bearer token header, and the API error text, never the
token, is returned on failure.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.request
from dataclasses import dataclass

WHATSAPP_API_BASE = "https://graph.facebook.com/v21.0"
SLACK_API_BASE = "https://slack.com/api"

_DECISION_RE = re.compile(r"^\s*(approve|reject)\s+([A-Za-z0-9-]+)\s*$", re.IGNORECASE)


def normalize_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def parse_decision(text: str) -> tuple[str, str] | None:
    match = _DECISION_RE.match(text or "")
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def verify_whatsapp_signature(raw: bytes, app_secret: str, header: str) -> bool:
    if not app_secret:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _post_json(url: str, token: str, payload: dict, post=None) -> dict:
    if post is not None:
        return post(url, token, payload)
    data = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=data, method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


@dataclass
class MessagingConfig:
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_owner: str = ""
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    # Slack Incoming Webhook URLs are destination-specific. Keys are Slack
    # conversation IDs (C..., D..., or G...), values are secret URLs.
    slack_webhooks: dict[str, str] | None = None

    @classmethod
    def from_env(cls, env=None) -> "MessagingConfig":
        env = env if env is not None else os.environ

        def get(*names: str) -> str:
            for name in names:
                value = env.get(name, "")
                if value:
                    return value
            return ""

        webhooks = {
            key[len("AIDON_SLACK_WEBHOOK_"):]: value
            for key, value in env.items()
            if key.startswith("AIDON_SLACK_WEBHOOK_") and value
        }
        return cls(
            # Old names only. A duplicate new name for the
            # same setting was removed, not kept as an alias.
            whatsapp_token=get("AIDON_WHATSAPP_TOKEN"),
            whatsapp_phone_id=get("AIDON_WHATSAPP_PHONE_ID"),
            whatsapp_verify_token=get("AIDON_WHATSAPP_VERIFY_TOKEN"),
            whatsapp_app_secret=get("AIDON_WHATSAPP_APP_SECRET"),
            whatsapp_owner=get("AIDON_WHATSAPP_OWNER"),
            slack_bot_token=get("SLACK_BOT_TOKEN"),
            slack_signing_secret=get("SLACK_SIGNING_SECRET"),
            slack_webhooks=webhooks,
        )

    def whatsapp_configured(self) -> bool:
        return bool(self.whatsapp_token and self.whatsapp_phone_id)

    def slack_configured(self) -> bool:
        return bool(self.slack_bot_token)

    def whatsapp_missing(self) -> list[str]:
        missing = []
        if not self.whatsapp_token:
            missing.append("AIDON_WHATSAPP_TOKEN")
        if not self.whatsapp_phone_id:
            missing.append("AIDON_WHATSAPP_PHONE_ID")
        if not self.whatsapp_verify_token:
            missing.append("AIDON_WHATSAPP_VERIFY_TOKEN")
        return missing


def send_whatsapp(config: MessagingConfig, to: str, text: str, post=None) -> dict:
    if not config.whatsapp_configured():
        return {"ok": False, "error": "AIDON_WHATSAPP_TOKEN / AIDON_WHATSAPP_PHONE_ID not set"}
    if not to:
        return {"ok": False, "error": "no recipient"}
    url = f"{WHATSAPP_API_BASE}/{config.whatsapp_phone_id}/messages"
    payload = {"messaging_product": "whatsapp", "to": to,
               "type": "text", "text": {"body": text}}
    try:
        result = _post_json(url, config.whatsapp_token, payload, post=post)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if result.get("error"):
        return {"ok": False, "error": str(result["error"])}
    messages = result.get("messages") or [{}]
    return {"ok": True, "message_id": messages[0].get("id", "")}


def send_slack(config: MessagingConfig, channel: str, text: str,
               thread_ts: str | None = None, post=None) -> dict:
    webhook = (config.slack_webhooks or {}).get(str(channel), "")
    if webhook:
        try:
            # Incoming Webhooks are already bound to their destination, so
            # the channel and thread fields are intentionally not sent.
            result = _post_json(webhook, "", {"text": text}, post=post)
            if isinstance(result, dict) and result.get("error"):
                return {"ok": False, "error": str(result["error"])}
            return {"ok": True, "webhook": True}
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}
    if not config.slack_configured():
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    if not channel:
        return {"ok": False, "error": "no channel"}
    url = f"{SLACK_API_BASE}/chat.postMessage"
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    try:
        result = _post_json(url, config.slack_bot_token, payload, post=post)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error", "unknown error"))}
    return {"ok": True, "ts": result.get("ts", "")}


def poll_slack_once(config, channel: str, last_ts: str = "", fetch=None) -> tuple[list[dict], str]:
    """Polling mode, old Slack, for when no public webhook URL exists.

    fetch is an injected Slack API caller, taking (url, token, params)
    and returning the JSON response. In production it calls
    conversations.history with the bot token. It returns
    (new user messages, newest ts), where a message is a dict
    with text, channel, and ts. Bot messages are skipped, and
    messages at or before last_ts are skipped, so a poll never
    re-dispatches the same message twice.
    """
    if fetch is None:
        import urllib.request, json as _json
        def fetch(url, token, params):
            req = urllib.request.Request(
                f"{url}?channel={params['channel']}&oldest={params.get('oldest', '')}",
                headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as response:
                return _json.loads(response.read().decode())
    result = fetch(f"{SLACK_API_BASE}/conversations.history", config.slack_bot_token,
                   {"channel": channel, "oldest": last_ts})
    messages = []
    newest = last_ts
    for message in result.get("messages", []):
        if message.get("bot_id") or message.get("subtype") == "bot_message":
            continue
        ts = str(message.get("ts", ""))
        if ts <= last_ts:
            continue
        messages.append({"text": str(message.get("text", "")), "channel": channel, "ts": ts})
        if ts > newest:
            newest = ts
    messages.reverse()
    return messages, newest


def download_whatsapp_media(config, media_id: str, get=None) -> bytes:
    """Download WhatsApp media bytes, via the Graph API, two steps.

    Step 1: GET /<media_id> with the WhatsApp token, returns a
    download URL. Step 2: GET that URL with the same token,
    returns the bytes. get is injectable for tests, taking
    (url, token) and returning bytes or a dict with a url.
    """
    if get is None:
        import urllib.request, json as _json
        def get(url, token):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read()
            try:
                return _json.loads(raw.decode())
            except Exception:
                return raw
    info = get(f"https://graph.facebook.com/v21.0/{media_id}", config.whatsapp_token)
    url = info.get("url", "") if isinstance(info, dict) else ""
    if not url:
        raise ValueError("WhatsApp media lookup did not return a download URL")
    data = get(url, config.whatsapp_token)
    if isinstance(data, dict):
        raise ValueError("WhatsApp media download did not return bytes")
    return data
