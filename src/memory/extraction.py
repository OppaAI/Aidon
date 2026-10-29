"""Background fact extraction for episodic and semantic memory.

After one chat turn pair (user prompt + assistant response), or after
a whole agentic run, a background thread asks the strong model for
what is durable in the exchange: atomic facts and interpretive
understandings. Either list may be empty — a turn with nothing
durable records nothing, which is a normal outcome, not an error.

Facts and understandings go to the episodic or semantic collection
by the scope the model assigns. This mechanism is only for episodic
and semantic memory. Procedural memory (DAG learning, experience
records) is a separate mechanism and is untouched here.

Strong-model guarantee: the extraction call goes through
``chat_phase("")`` when the client offers it, which routes to the
strong tier on every router (RoutedClient treats "" as
not-a-fast-phase; RouterClient falls back to its default, the main
client). Plain ``chat`` is the fallback for unrouted clients, which
are the strong client already. The fast tier never extracts.

The thread is a daemon: it must never block the reply, delay
shutdown, or fail the run that already finished.
"""

from __future__ import annotations

import json
import os
import re
import threading

_PROMPT = """Extract what is durable from this exchange. Return JSON only, no other text:
{"facts": [{"subject": "...", "predicate": "...", "value": "...", "scope": "episodic|semantic", "importance": 0.0-1.0}],
 "understandings": [{"text": "...", "scope": "episodic|semantic", "importance": 0.0-1.0}]}
Facts are atomic, verifiable statements: preferences, biographical facts, project constraints, decisions.
Understandings are interpretive takeaways: what the user is trying to achieve, their goals, attitudes, why they want something.
Scope episodic = specific to this project or conversation; semantic = generally true about the user.
A turn may hold facts and understandings at the same time, or neither. Either list may be empty. Return empty lists when nothing durable is present. Do not invent anything not in the exchange.
Exchange:
"""


def extraction_enabled() -> bool:
    """Off only when AIDON_MEMORY_EXTRACT names a false value."""
    value = os.environ.get("AIDON_MEMORY_EXTRACT", "")
    if not value.strip():
        return True
    return value.strip().lower() not in ("0", "false", "no", "off")


def _scope(value) -> str:
    return "semantic" if str(value or "").strip().lower() == "semantic" else "episodic"


def _importance(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.5
    if number != number:  # NaN
        return 0.5
    return max(0.0, min(1.0, number))


def parse_extraction(text: str) -> dict:
    """Parse extractor output into facts + understandings, never raising.

    Strict JSON first; on failure, the first {...} block (the same
    fallback shape used for model JSON elsewhere); on failure, empty.
    Malformed items are dropped, never fatal.
    """
    data = None
    try:
        data = json.loads(text or "")
    except Exception:
        match = re.search(r"\{.*\}", text or "", re.S)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    if not isinstance(data, dict):
        return {"facts": [], "understandings": []}
    facts = []
    for item in data.get("facts") or []:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject") or "").strip()
        value = str(item.get("value") or "").strip()
        if not subject or not value:
            continue
        predicate = str(item.get("predicate") or "is").strip() or "is"
        facts.append({"subject": subject, "predicate": predicate, "value": value,
                      "scope": _scope(item.get("scope")),
                      "importance": _importance(item.get("importance"))})
    understandings = []
    for item in data.get("understandings") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        understandings.append({"text": text,
                               "scope": _scope(item.get("scope")),
                               "importance": _importance(item.get("importance"))})
    return {"facts": facts, "understandings": understandings}


def _strong_chat(client, messages) -> str:
    """One model call on the strong tier, whatever client shape arrives."""
    chat_phase = getattr(client, "chat_phase", None)
    if callable(chat_phase):
        try:
            return chat_phase("", messages, sampling=None)
        except TypeError:
            return chat_phase("", messages)
    try:
        return client.chat(messages, sampling=None)
    except TypeError:
        return client.chat(messages)


def extract_from_text(exchange_text: str, chat) -> dict:
    """One strong-model call; empty result when the call fails."""
    from ..system.model_client import Message

    try:
        reply = _strong_chat(chat, [Message("user", _PROMPT + exchange_text)])
    except Exception:
        return {"facts": [], "understandings": []}
    return parse_extraction(reply)


def record_extraction(memory, project_id: str, result: dict) -> int:
    """Record facts + understandings into their scoped collections."""
    count = 0
    for fact in result.get("facts") or []:
        text = f"{fact['subject']} {fact['predicate']} {fact['value']}"
        payload = {"kind": "fact", "subject": fact["subject"],
                   "predicate": fact["predicate"], "value": fact["value"],
                   "scope": fact["scope"], "importance": fact["importance"],
                   "source": "extraction"}
        if fact["scope"] == "episodic":
            payload["project_id"] = project_id
        try:
            memory.record(fact["scope"], text, payload)
            count += 1
        except Exception:
            continue
    for item in result.get("understandings") or []:
        payload = {"kind": "understanding", "scope": item["scope"],
                   "importance": item["importance"], "source": "extraction"}
        if item["scope"] == "episodic":
            payload["project_id"] = project_id
        try:
            memory.record(item["scope"], item["text"], payload)
            count += 1
        except Exception:
            continue
    return count


def spawn_extraction(memory, project_id: str, exchange_text: str, chat):
    """Start the background extraction thread; None when skipped.

    Skipped (no thread, no call) when extraction is disabled, when
    there is no memory, or when there is no chat client. The returned
    thread lets callers join in tests; production never joins it.
    """
    if not extraction_enabled() or memory is None or chat is None:
        return None

    def _run():
        try:
            result = extract_from_text(exchange_text, chat)
            record_extraction(memory, project_id, result)
        except Exception:
            pass

    thread = threading.Thread(target=_run, daemon=True, name="memory-extract")
    thread.start()
    return thread
