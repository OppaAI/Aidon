"""Webchat fast path helper.

A webchat goal needs a web lookup, but produces no artifact
and runs no tools, so it should not go through the full
plan, sandbox, verify, and approval pipeline. This helper
is the fast path: run a researcher, then answer from the
research facts only.

Rules:
- researcher is a callable taking the goal and returning a
  list of fact strings. It is the only source of facts.
- If the researcher returns no facts, this function returns
  a fixed "facts were not found" reply and does not call
  the model at all, so no fact can be invented in that case.
- If facts are present, the model is called with the goal
  plus the facts, with an explicit instruction to use only
  those facts. The model call uses chat_phase("webchat", ...)
  when the client provides it, otherwise chat(...).
- This function itself never invents a fact: it only copies
  researcher facts into the prompt, verbatim.
"""

from __future__ import annotations


def _make_message(text: str):
    try:
        from .model_client import Message

        return Message(role="user", content=text)
    except Exception:
        # Fallback for a client that accepts a plain object
        # with role and content attributes.
        class _SimpleMessage:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        return _SimpleMessage("user", text)


def webchat_reply(goal: str, researcher, client) -> str:
    """Return a webchat reply for a goal, from researcher facts only."""
    if not goal or not str(goal).strip():
        raise ValueError("goal is required")
    if researcher is None:
        raise ValueError("researcher is required")
    if client is None:
        raise ValueError("client is required")
    facts = researcher(goal)
    if facts is None:
        facts = []
    if not isinstance(facts, (list, tuple)):
        facts = [str(facts)]
    facts = [str(fact) for fact in facts if str(fact).strip()]
    if not facts:
        return f"Facts were not found for this goal: {goal}."
    facts_text = "\n".join(f"- {fact}" for fact in facts)
    prompt = (
        f"Goal: {goal}\n"
        f"Research facts:\n{facts_text}\n\n"
        "Answer the goal using only the facts above. "
        "Do not invent any fact that is not in the facts above."
    )
    message = _make_message(prompt)
    if hasattr(client, "chat_phase"):
        try:
            return str(client.chat_phase("webchat", [message]))
        except TypeError:
            return str(client.chat([message]))
    return str(client.chat([message]))
