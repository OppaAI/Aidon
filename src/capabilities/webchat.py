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
        from ..system.model_client import Message

        return Message(role="user", content=text)
    except Exception:
        # Fallback for a client that accepts a plain object
        # with role and content attributes.
        class _SimpleMessage:
            def __init__(self, role, content):
                self.role = role
                self.content = content

        return _SimpleMessage("user", text)


def _local_state_words(goal: str) -> bool:
    """True when the goal asks about the user's own state."""
    import re

    words = ("budget", "list", "plan", "notes", "file", "saved", "my")
    text = str(goal).lower()
    return any(re.search(r"\b" + word + r"\b", text) for word in words)


def _named_file(goal: str):
    """The first filename-like token in the goal, or None."""
    import re

    match = re.search(
        r"[\w./\-]+\.(?:md|txt|csv|json|pdf|html|py|yaml|yml|toml)\b",
        str(goal))
    return match.group(0) if match else None


def _call_model(client, prompt: str, on_token=None) -> str:
    try:
        from ..agentic.prompts import runtime_model_inventory
        prompt = runtime_model_inventory() + "\n\n" + prompt
    except Exception:
        # The fast path must remain usable with minimal test clients.
        pass
    message = _make_message(prompt)
    try:
        from ..agentic.think_effort import sampling_for
        sampling = sampling_for("webchat")
    except Exception:
        sampling = None
    if on_token is not None and hasattr(client, "chat_stream"):
        chunks = []
        try:
            stream = client.chat_stream("webchat", [message], sampling=sampling)
        except TypeError:
            stream = client.chat_stream([message], sampling=sampling)
        for chunk in stream:
            text = str(chunk)
            chunks.append(text)
            on_token(text)
        return "".join(chunks)
    if hasattr(client, "chat_phase"):
        try:
            return str(client.chat_phase("webchat", [message], sampling=sampling))
        except TypeError:
            try:
                return str(client.chat([message], sampling=sampling))
            except TypeError:
                return str(client.chat([message]))
    try:
        return str(client.chat([message], sampling=sampling))
    except TypeError:
        return str(client.chat([message]))


def webchat_reply_with_local(goal: str, researcher, client,
                             read_file=None, today=None, context: str = "") -> str:
    """Webchat reply that may also answer from a local file.

    When the goal asks about the user's own state (budget,
    list, saved plan, notes, file) AND a ``read_file`` callable
    is given, the prompt carries the question-date note (when
    a date is implied) and an explicit instruction: answer
    from the local file and the research facts, never from
    conversation memory, and the date in the question is the
    authority over any date in a learned fact. ``read_file``
    is the caller's confined read tool: confinement to the
    workdir is the caller's job, this helper never opens a
    path itself.

    When no local file is in play, this falls back to
    ``webchat_reply`` exactly, unchanged behaviour.
    """
    if not goal or not str(goal).strip():
        raise ValueError("goal is required")
    if researcher is None:
        raise ValueError("researcher is required")
    if client is None:
        raise ValueError("client is required")
    if read_file is None or not _local_state_words(goal):
        return webchat_reply(goal, researcher, client, context=context)
    facts = researcher(goal)
    if facts is None:
        facts = []
    if not isinstance(facts, (list, tuple)):
        facts = [str(facts)]
    facts = [str(fact) for fact in facts if str(fact).strip()]
    local_text = ""
    filename = _named_file(goal)
    if filename is not None:
        try:
            local_text = str(read_file(filename) or "")
        except Exception:
            local_text = ""
    if not facts and not local_text:
        return f"Facts were not found for this goal: {goal}."
    from ..agentic.question_dates import question_date_note

    parts = [f"Goal: {goal}\n"]
    if str(context).strip():
        parts.insert(0,
                     "Recent conversation context (reference resolution only):\n"
                     f"{context.strip()}\n\n")
    if facts:
        parts.append("Research facts:\n" + "\n".join(f"- {fact}" for fact in facts) + "\n")
    if local_text:
        parts.append(f"Local file {filename}:\n{local_text}\n")
    note = question_date_note(goal, today=today)
    if note:
        parts.append(note + "\n")
    parts.append(
        "Answer from the local file and the research facts above, "
        "never from conversation memory. "
        "The date in the question is the authority over any date "
        "in a learned fact. "
        "Do not invent any fact that is not in the facts above.")
    return _call_model(client, "\n".join(parts))


def read_only_tools(workdir) -> dict:
    """A read-only toolset: a confined "read" tool, nothing else.

    No write, no shell, no calculate. Reads are confined to
    the workdir: a path outside it raises ValueError, never
    followed. Uses ToolRegistry when importable, plain Path
    reads otherwise.
    """
    from pathlib import Path

    root = Path(workdir).resolve()

    def read(file: str) -> str:
        target = (root / str(file)).resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path outside workdir: {file}")
        try:
            from ..capabilities.tools import ToolRegistry

            return str(ToolRegistry(root).call("read", {"file": str(file)}))
        except ValueError:
            raise
        except Exception:
            return target.read_text(encoding="utf-8")

    return {"read": read}


def webchat_reply(goal: str, researcher, client, on_token=None,
                  context: str = "") -> str:
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
    context_block = (
        "Recent conversation context (use only to resolve references such as "
        "'this' or 'the rules'; it is not a source of facts):\n"
        f"{context.strip()}\n\n" if str(context).strip() else "")
    prompt = (
        f"{context_block}"
        f"Goal: {goal}\n"
        f"Research facts:\n{facts_text}\n\n"
        "Use the conversation context only to understand what the goal refers "
        "to, then answer using only the research facts above. "
        "Do not invent any fact that is not in the facts above."
    )
    return _call_model(client, prompt, on_token=on_token)
