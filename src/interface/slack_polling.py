"""Reliable dispatch helpers for the WebUI's Slack history poller."""

from __future__ import annotations

import threading
import traceback


def discover_slack_threads(config, channel, cursors, since, fetch=None):
    """Find updated threads among the channel's 100 most recent roots.

    History excludes replies and an old root can receive a new reply.
    Use each thread's checkpoint, falling back to the channel checkpoint,
    so discovery never replays conversations from before polling began.
    """
    import json
    import urllib.request
    from decimal import Decimal
    from urllib.parse import urlencode
    from .messaging import SlackRateLimited, SLACK_API_BASE, _slack_api_error

    if fetch is None:
        def fetch(url, token, params):
            request = urllib.request.Request(
                url + "?" + urlencode(params),
                headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(request, timeout=15) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    raise SlackRateLimited(exc.headers.get("Retry-After", "60")) from None
                raise RuntimeError(f"Slack thread discovery failed: HTTP {exc.code}") from None

    result = fetch(f"{SLACK_API_BASE}/conversations.history",
                   config.slack_bot_token, {"channel": channel, "limit": 100})
    if not isinstance(result, dict):
        raise RuntimeError("Slack thread discovery returned an invalid response")
    if not result.get("ok"):
        if result.get("error") in ("ratelimited", "rate_limited"):
            raise SlackRateLimited()
        raise RuntimeError("Slack thread discovery failed: " + _slack_api_error(result))
    threads = []
    for message in result.get("messages", []):
        root = str(message.get("ts", ""))
        latest = str(message.get("latest_reply", "") or "")
        if not root or not latest:
            continue
        checkpoint = cursors.get(f"thread:{channel}:{root}", since)
        if Decimal(latest) > Decimal(checkpoint or "0"):
            threads.append({"thread_ts": root, "checkpoint": checkpoint})
    return threads


def configured_slack_channels(value: str) -> tuple[str, ...]:
    """Parse the comma-separated channel setting, preserving order."""
    return tuple(dict.fromkeys(
        channel.strip() for channel in str(value or "").split(",")
        if channel.strip()))


def process_polled_messages(messages, handle, checkpoint) -> None:
    """Handle each message before advancing its durable timestamp cursor."""
    for message in messages:
        handle(message)
        timestamp = str(message.get("ts", ""))
        if timestamp:
            checkpoint(timestamp)


def dispatch_slack_message(dispatcher, event, *, channel: str, thread_ts: str,
                           sender, ack_after: float = 15, home=None) -> None:
    """Dispatch a polled message and return its result to the same thread.

    Exceptions raised in the worker thread used to disappear from the poll
    loop, leaving the message cursor advanced and the Slack user with no
    response. Capture them and send a safe, useful failure reply instead.
    """
    replies: list[str] = []
    failures: list[Exception] = []

    def _dispatch():
        try:
            dispatcher.dispatch(event)
        except Exception as exc:  # surfaced to the Slack user below
            frames = traceback.extract_tb(exc.__traceback__)
            locations = " -> ".join(
                f"{frame.filename.rsplit('/', 1)[-1]}:"
                f"{frame.lineno}:{frame.name}"
                for frame in frames[-6:])
            detail = (f"{type(exc).__name__}; "
                      f"stack: {locations or 'unavailable'}")
            print(f"Slack goal handler failed: {detail}")
            if home is not None:
                try:
                    from ..system.diagnostics import log_run_error
                    log_run_error(
                        home, str(event.payload.get("run_id", "") or ""),
                        "slack_dispatch", detail)
                except Exception:
                    pass
            failures.append(exc)

    event.reply = replies.append
    worker = threading.Thread(target=_dispatch, daemon=True)
    worker.start()
    worker.join(max(0, ack_after))
    if worker.is_alive():
        sender(channel, "Working on it…", thread_ts)
    worker.join()

    if failures:
        sender(
            channel,
            "Aidon couldn't process that message because its run encountered "
            "an internal error. Check the Aidon logs, then send it again.",
            thread_ts,
        )
        return
    for reply in replies:
        sender(channel, reply, thread_ts)


def load_poll_cursors(path, channels, now=None):
    """Resume saved history; a new channel starts now, never replays old goals.

    A corrupt file needs owner recovery. Silently resetting it could execute
    arbitrary historical goals whose effects have already happened.
    """
    import json
    import re
    import time

    if path.exists():
        try:
            cursors = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError("Slack cursor file is unreadable; restore it before polling") from exc
        if (not isinstance(cursors, dict)
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value)
                       for value in cursors.values())):
            raise ValueError("Slack cursor file is invalid; restore it before polling")
    else:
        cursors = {}
    timestamp = str(time.time() if now is None else now)
    for channel in channels:
        cursors.setdefault(channel, timestamp)
    save_poll_cursors(path, cursors)
    return cursors


def save_poll_cursors(path, cursors):
    import json
    import os

    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        json.dump(cursors, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
