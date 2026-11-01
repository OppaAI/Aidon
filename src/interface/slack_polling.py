"""Reliable dispatch helpers for the WebUI's Slack history poller."""

from __future__ import annotations

import threading


def process_polled_messages(messages, handle, checkpoint) -> None:
    """Handle each message before advancing its durable timestamp cursor."""
    for message in messages:
        handle(message)
        timestamp = str(message.get("ts", ""))
        if timestamp:
            checkpoint(timestamp)


def dispatch_slack_message(dispatcher, event, *, channel: str, thread_ts: str,
                           sender, ack_after: float = 15) -> None:
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
            print(f"Slack goal handler failed: {type(exc).__name__}")
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
