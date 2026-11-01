"""Reliable dispatch helpers for the WebUI's Slack history poller."""

from __future__ import annotations

import threading


def process_polled_messages(messages, handle, checkpoint) -> None:
    """Handle each message before advancing its durable timestamp cursor."""
    for message in messages:
        try:
            handle(message)
        finally:
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
