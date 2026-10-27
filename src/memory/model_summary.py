"""Model-written rolling summary, with a code-written fallback that never crashes.

A code-written summary is safe but flat: it lists the goal,
the files, and the status, and it never synthesises what
changed. A model-written summary can compress several runs
into one paragraph, keeping the facts that matter and dropping
the rest, which is what a later run actually needs to read.

How it works:
- rolling_summary_model(previous_summary, new_events, client)
  builds one user message containing the previous summary
  and the new events, and asks the model to write a short
  rolling summary, preserving facts, adding none.
- If the client has chat_phase, it is used with phase
  "summarize", so a phase-routed client can send this to
  the fast tier. Otherwise client.chat is used. Either way
  the message shape is the same.
- If the client is None, raises, or returns empty text,
  the function falls back to a code-written summary:
  previous summary, plus the new events joined by "; ",
  truncated to 2000 characters. It never raises for a model
  failure, because a summary is a convenience, not a
  deliverable, and a failed summary must not fail a run.

The model output is returned as-is, stripped, not verified
here. A caller that needs the summary to be true must
check it against the events, the same rule as any model
output: a model call is never proof on its own.
"""

from __future__ import annotations

from ..system.model_client import Message


def _fallback(previous_summary: str, new_events: list[str]) -> str:
    events_text = "; ".join(event for event in new_events if event)
    if previous_summary and events_text:
        text = f"{previous_summary} | {events_text}"
    elif previous_summary:
        text = previous_summary
    else:
        text = events_text
    return text[:2000]


def rolling_summary_model(previous_summary: str, new_events: list[str], client) -> str:
    """Write a rolling summary with the model, fall back to code on any failure."""
    previous_summary = previous_summary or ""
    new_events = [event for event in (new_events or []) if event]
    if client is None:
        return _fallback(previous_summary, new_events)
    events_text = "\n".join(f"- {event}" for event in new_events)
    prompt = (
        "Write a short rolling summary, under 500 characters, "
        "preserving facts, adding no new facts.\n"
        f"Previous summary: {previous_summary}\n"
        f"New events:\n{events_text}"
    )
    messages = [Message("user", prompt)]
    try:
        if hasattr(client, "chat_phase"):
            result = client.chat_phase("summarize", messages)
        else:
            result = client.chat(messages)
    except Exception:
        return _fallback(previous_summary, new_events)
    if not isinstance(result, str) or not result.strip():
        return _fallback(previous_summary, new_events)
    return result.strip()
