"""WhatsApp and Slack, two-way, following Aidon, with old key names.

Old key names are canonical here, exactly as in Aidon:
- WhatsApp: AIDON_WHATSAPP_TOKEN, AIDON_WHATSAPP_PHONE_ID,
  AIDON_WHATSAPP_VERIFY_TOKEN, AIDON_WHATSAPP_APP_SECRET,
  AIDON_WHATSAPP_OWNER
- Slack: SLACK_BOT_TOKEN, plus AIDON_SLACK_SIGNING_SECRET, which is
  Slack's own name, not an Aidon name, because old Slack
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
Slack has no owner gate in Aidon, it has a bot
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
from pathlib import Path

WHATSAPP_API_BASE = "https://graph.facebook.com/v21.0"
SLACK_API_BASE = "https://slack.com/api"

_DECISION_RE = re.compile(r"^\s*(approve|reject)\s+([A-Za-z0-9-]+)\s*$", re.IGNORECASE)


def normalize_number(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def _transport_error(exc: Exception) -> str:
    """Return a useful, secret-safe HTTP diagnostic for messaging failures."""
    import urllib.error as _urlerror
    if isinstance(exc, _urlerror.HTTPError):
        detail = ""
        try:
            payload = json.loads(exc.read(8192).decode("utf-8", errors="replace"))
            code = str(payload.get("error", "")) if isinstance(payload, dict) else ""
            if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
                detail = f": {code}"
        except Exception:
            pass
        return f"HTTP {exc.code}{detail}"
    return type(exc).__name__


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
    slack_outbound_channel: str = ""
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
            slack_signing_secret=get("AIDON_SLACK_SIGNING_SECRET"),
            slack_outbound_channel=get("AIDON_SLACK_OUTBOUND_CHANNEL",
                                       "AIDON_SLACK_INBOUND_CHANNEL").split(",")[0].strip(),
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
        return {"ok": False, "error": _transport_error(exc)}
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
            return {"ok": False, "error": _transport_error(exc)}
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
        return {"ok": False, "error": _transport_error(exc)}
    if not result.get("ok"):
        return {"ok": False, "error": str(result.get("error", "unknown error"))}
    return {"ok": True, "ts": result.get("ts", "")}


def _post_multipart(url: str, token: str, fields: dict, file_bytes: bytes,
                    filename: str, upload=None) -> dict:
    """POST one file as multipart/form-data, stdlib only.

    fields are plain form fields; the file rides as the
    "file" part. upload is injectable for tests, taking
    (url, token, fields, file_bytes, filename) and returning
    a parsed dict. Slack's external upload URL is already
    authenticated, so callers pass token="" for that step.
    """
    if upload is not None:
        return upload(url, token, fields, file_bytes, filename)
    import uuid as _uuid
    boundary = _uuid.uuid4().hex
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode())
    parts.append(b"Content-Type: application/octet-stream\r\n\r\n")
    parts.append(file_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=b"".join(parts),
                                     method="POST", headers=headers)
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    try:
        return json.loads(raw.decode())
    except Exception:
        return {"ok": True, "raw": raw[:200].decode(errors="replace")}


def send_slack_file(config, channel: str, file_path, title: str = "",
                    post=None, upload=None) -> dict:
    """Upload a file to a Slack channel, files.uploadV2 three steps.

    Step 1 files.getUploadURLExternal reserves the upload,
    step 2 posts the bytes to the returned URL, step 3
    files.completeUploadExternal shares it in the channel.
    Incoming webhooks cannot upload files, so a bot token
    is required. Links need no upload — a URL in a text
    message autolinks in Slack — this is for real files
    and media the phone cannot reach over localhost.
    """
    if not config.slack_configured():
        return {"ok": False, "error": "SLACK_BOT_TOKEN not set"}
    if not channel:
        return {"ok": False, "error": "no channel"}
    path = Path(file_path) if not isinstance(file_path, Path) else file_path
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    data = path.read_bytes()
    try:
        reserved = _post_json(f"{SLACK_API_BASE}/files.getUploadURLExternal",
                              config.slack_bot_token,
                              {"filename": path.name, "length": len(data)}, post=post)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if not reserved.get("ok"):
        return {"ok": False, "error": str(reserved.get("error", "upload URL failed"))}
    try:
        _post_multipart(reserved["upload_url"], "", {}, data, path.name, upload=upload)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    try:
        done = _post_json(f"{SLACK_API_BASE}/files.completeUploadExternal",
                          config.slack_bot_token,
                          {"files": [{"id": reserved["file_id"],
                                      "title": title or path.name}],
                           "channel_id": channel}, post=post)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if not done.get("ok"):
        return {"ok": False, "error": str(done.get("error", "upload completion failed"))}
    return {"ok": True, "file_id": reserved["file_id"]}


_WHATSAPP_MEDIA_TYPES = {
    "image": {"jpg", "jpeg", "png", "gif", "webp"},
    "video": {"mp4", "3gp"},
    "audio": {"mp3", "ogg", "aac", "m4a", "opus"},
}


def send_whatsapp_media(config, to: str, file_path, caption: str = "",
                        post=None, upload=None) -> dict:
    """Send a file or media over WhatsApp, Graph media API two steps.

    Step 1 uploads the bytes to /<phone-number-id>/media and
    returns a media id; step 2 sends a message carrying that
    id. The type follows the extension — image, video, audio,
    everything else document — matching the media API's own
    types. This is why no Dropbox-style link host is needed:
    the bytes live in WhatsApp itself, and the existing
    messaging token already covers media upload.
    """
    if not config.whatsapp_configured():
        return {"ok": False, "error": "AIDON_WHATSAPP_TOKEN / AIDON_WHATSAPP_PHONE_ID not set"}
    if not to:
        return {"ok": False, "error": "no recipient"}
    path = Path(file_path) if not isinstance(file_path, Path) else file_path
    if not path.is_file():
        return {"ok": False, "error": f"file not found: {path}"}
    ext = path.suffix.lower().lstrip(".")
    media_type = "document"
    for kind, exts in _WHATSAPP_MEDIA_TYPES.items():
        if ext in exts:
            media_type = kind
            break
    try:
        uploaded = _post_multipart(
            f"{WHATSAPP_API_BASE}/{config.whatsapp_phone_id}/media",
            config.whatsapp_token, {"messaging_product": "whatsapp"},
            path.read_bytes(), path.name, upload=upload)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    media_id = uploaded.get("id", "")
    if not media_id:
        return {"ok": False, "error": f"media upload failed: {uploaded}"}
    body: dict = {"messaging_product": "whatsapp", "to": to, "type": media_type}
    media_body: dict = {"id": media_id}
    if media_type in ("image", "video") and caption:
        media_body["caption"] = caption
    if media_type == "document":
        media_body["filename"] = path.name
        if caption:
            media_body["caption"] = caption
    body[media_type] = media_body
    try:
        result = _post_json(f"{WHATSAPP_API_BASE}/{config.whatsapp_phone_id}/messages",
                            config.whatsapp_token, body, post=post)
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}
    if result.get("error"):
        return {"ok": False, "error": str(result["error"])}
    messages = result.get("messages") or [{}]
    return {"ok": True, "message_id": messages[0].get("id", ""), "media_id": media_id}


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
    if isinstance(result, dict) and result.get("ok") is False:
        code = str(result.get("error", "api_error"))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", code):
            code = "api_error"
        raise RuntimeError(f"Slack conversations.history failed: {code}")
    messages = []
    newest = last_ts
    for message in result.get("messages", []):
        ts = str(message.get("ts", ""))
        if ts > newest:
            newest = ts
        if message.get("bot_id") or message.get("subtype") == "bot_message":
            continue
        if ts <= last_ts:
            continue
        messages.append({"text": str(message.get("text", "")), "channel": channel, "ts": ts})
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


def download_slack_file(token: str, file_url_or_id: str, get=None,
                        timeout_seconds: int = 15) -> bytes:
    """Download a Slack shared file's bytes, via its private URL.

    file_url_or_id is either the file's url_private (as carried in
    an event payload) or a bare file id, resolved first through
    files.info. The token travels only in the Authorization
    header, never in a URL, a log line, or an error message;
    only https URLs are fetched, and every request is
    timeout-bounded. get is injectable for tests, taking
    (url, token) and returning bytes or a dict. Returns the file
    bytes; raises ValueError on any refusal or failed lookup or
    download. This helper is not wired into any handler: a caller
    that holds a bot token uses it explicitly.
    """
    if not token or not file_url_or_id:
        raise ValueError("Slack file download needs a token and a file URL or id")
    if get is None:
        import urllib.request, json as _json
        def get(url, token, _timeout=timeout_seconds):
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=_timeout) as response:
                raw = response.read()
            try:
                return _json.loads(raw.decode())
            except Exception:
                return raw
    url = str(file_url_or_id)
    if "://" not in url:
        # Bare file id: resolve through files.info, Bearer header
        # only, never a token query parameter.
        info = get(f"https://slack.com/api/files.info?file={url}", token)
        if not isinstance(info, dict):
            raise ValueError("Slack files.info did not return file metadata")
        file_info = info.get("file", {}) if isinstance(info.get("file"), dict) else {}
        url = file_info.get("url_private", "") or ""
        if not url:
            raise ValueError("Slack files.info did not return a private URL")
    from urllib.parse import urlparse
    if urlparse(url).scheme != "https":
        raise ValueError("Slack file download refuses non-https URLs")
    data = get(url, token)
    if isinstance(data, dict):
        raise ValueError("Slack file download did not return bytes")
    return data
