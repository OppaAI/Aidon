"""Surface links: WebUI and Slack unified, WhatsApp mirror opt-in.

WebUI and Slack are concurrent and unified: a run started on
either surface has the same input and output on the other,
once linked. Slack activity is mirrored into the WebUI
conversation store automatically (one conversation per
channel, id ``slack:<channel>``), so WebUI shows Slack
threads without any setup. WebUI to Slack needs one explicit
link, because Aidon cannot guess which channel a WebUI
conversation belongs to: ``/mirror slack #channel`` in the
WebUI chat, or ``/mirror webui <conversation-id>`` in Slack.

WhatsApp is different by owner rule: it is used when the
owner inputs there, and for output only when specified —
either a result-delivery suffix on the goal (already
supported), a practice report (specified by the practice
request), or ``/mirror whatsapp on`` for a conversation,
which routes that conversation's questions and results to
WhatsApp too. Approvals can already be given from any
surface (``approve <id>`` resolves a run started anywhere);
approval cards themselves are not pushed to WhatsApp,
because the gate carries no run context to filter them by.

A link is one row: a WebUI conversation id, an optional
Slack channel, and a WhatsApp mirror flag. A channel links
to at most one conversation and vice versa; linking a new
pair drops the conflicting old ones, so a channel never
fans out to two conversations.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_LINKS_FILE = "surface_links.json"

_MIRROR_RE = re.compile(
    r"^/mirror\s+(slack|whatsapp|webui)(?:\s+(.*?))?\s*$", re.IGNORECASE)


def _path(home: Path) -> Path:
    return Path(home) / _LINKS_FILE


def load_links(home: Path) -> list[dict]:
    """Return the link rows, [] when the file is absent or broken."""
    try:
        data = json.loads(_path(home).read_text(encoding="utf-8"))
    except Exception:
        return []
    links = data.get("links", []) if isinstance(data, dict) else []
    return [link for link in links if isinstance(link, dict)]


def _save(home: Path, links: list[dict]) -> None:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"links": links}, indent=2), encoding="utf-8")
    tmp.replace(path)


def _normalize_channel(channel: str) -> str:
    return str(channel or "").strip().lstrip("#").strip()


def link_slack(home: Path, conversation_id: str, channel: str) -> dict:
    """Link a WebUI conversation to a Slack channel (both directions)."""
    channel = _normalize_channel(channel)
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id or not channel:
        raise ValueError("a conversation id and a channel are required")
    links = [link for link in load_links(home)
             if link.get("conversation") != conversation_id
             and link.get("slack") != channel]
    row = {"conversation": conversation_id, "slack": channel, "whatsapp": False}
    for link in load_links(home):
        if link.get("conversation") == conversation_id:
            row["whatsapp"] = bool(link.get("whatsapp", False))
    links.append(row)
    _save(home, links)
    return row


def unlink_slack(home: Path, conversation_id: str) -> bool:
    """Drop the Slack link for a conversation; True when one existed."""
    links = load_links(home)
    kept = [link for link in links
            if link.get("conversation") != conversation_id]
    if len(kept) == len(links):
        return False
    _save(home, kept)
    return True


def set_whatsapp_mirror(home: Path, conversation_id: str, enabled: bool) -> dict:
    """Mirror a conversation's questions and results to WhatsApp."""
    conversation_id = str(conversation_id or "").strip()
    if not conversation_id:
        raise ValueError("a conversation id is required")
    links = load_links(home)
    for link in links:
        if link.get("conversation") == conversation_id:
            link["whatsapp"] = bool(enabled)
            _save(home, links)
            return link
    row = {"conversation": conversation_id, "slack": "", "whatsapp": bool(enabled)}
    links.append(row)
    _save(home, links)
    return row


def slack_for_conversation(home: Path, conversation_id: str) -> str | None:
    """The Slack channel linked to a WebUI conversation, if any."""
    for link in load_links(home):
        if link.get("conversation") == conversation_id and link.get("slack"):
            return str(link["slack"])
    return None


def conversation_for_slack(home: Path, channel: str) -> str | None:
    """The WebUI conversation linked to a Slack channel, if any."""
    channel = _normalize_channel(channel)
    for link in load_links(home):
        if _normalize_channel(str(link.get("slack", ""))) == channel:
            return str(link.get("conversation", ""))
    return None


def whatsapp_mirrored_for_conversation(home: Path, conversation_id: str) -> bool:
    """True when a WebUI conversation mirrors to WhatsApp."""
    for link in load_links(home):
        if link.get("conversation") == conversation_id:
            return bool(link.get("whatsapp", False))
    return False


def whatsapp_mirrored_for_slack(home: Path, channel: str) -> bool:
    """True when a Slack channel mirrors to WhatsApp.

    Either through its linked WebUI conversation, or through
    its implicit ``slack:<channel>`` row (set from Slack itself
    before any WebUI link exists).
    """
    conversation = conversation_for_slack(home, channel)
    if conversation and whatsapp_mirrored_for_conversation(home, conversation):
        return True
    return whatsapp_mirrored_for_conversation(home, "slack:" + _normalize_channel(channel))


def waiter_keys_for_conversation(home: Path, conversation_id: str,
                                 whatsapp_owner: str = "") -> list[str]:
    """Extra waiter keys so a linked surface can answer a WebUI run's question."""
    keys: list[str] = []
    channel = slack_for_conversation(home, conversation_id)
    if channel:
        keys.append("slack:" + channel)
    if whatsapp_owner and whatsapp_mirrored_for_conversation(home, conversation_id):
        keys.append("whatsapp:" + str(whatsapp_owner))
    return keys


def waiter_keys_for_slack(home: Path, channel: str,
                          whatsapp_owner: str = "") -> list[str]:
    """Extra waiter keys so WebUI (and WhatsApp, when mirrored) can answer a Slack run."""
    keys: list[str] = []
    conversation = conversation_for_slack(home, channel)
    if conversation:
        keys.append(conversation)
    if whatsapp_owner and whatsapp_mirrored_for_slack(home, channel):
        keys.append("whatsapp:" + str(whatsapp_owner))
    return keys


def parse_mirror_command(text: str) -> dict | None:
    """Parse a /mirror command; None when the text is not one.

    Forms: ``/mirror slack #channel``, ``/mirror slack off``,
    ``/mirror webui <conversation-id>``, ``/mirror whatsapp on|off``,
    bare ``/mirror`` (status request). Any other ``/mirror ...``
    text also returns status, so a typo shows the current
    links instead of dispatching as a goal.
    """
    text = str(text or "").strip()
    match = _MIRROR_RE.match(text)
    if not match:
        lowered = text.lower()
        if lowered == "/mirror" or lowered.startswith("/mirror "):
            return {"action": "status"}
        return None
    target, arg = match.group(1).lower(), (match.group(2) or "").strip()
    if target == "slack":
        if arg.lower() == "off":
            return {"action": "unlink_slack"}
        if not arg:
            return {"action": "status"}
        return {"action": "link_slack", "channel": _normalize_channel(arg)}
    if target == "webui":
        if not arg:
            return {"action": "status"}
        return {"action": "link_webui", "conversation": arg}
    if arg.lower() in ("on", "off"):
        return {"action": "whatsapp", "enabled": arg.lower() == "on"}
    return {"action": "status"}


def mirror_status_text(home: Path, conversation_id: str = "",
                       channel: str = "") -> str:
    """One-line status for a /mirror status request."""
    if conversation_id:
        slack = slack_for_conversation(home, conversation_id)
        wa = whatsapp_mirrored_for_conversation(home, conversation_id)
        return (f"Mirror: Slack {'#' + slack if slack else 'not linked'}; "
                f"WhatsApp {'on' if wa else 'off'}.")
    if channel:
        conversation = conversation_for_slack(home, channel)
        wa = whatsapp_mirrored_for_slack(home, channel)
        return (f"Mirror: WebUI conversation {conversation or 'not linked'}; "
                f"WhatsApp {'on' if wa else 'off'}.")
    return "Mirror: no conversation or channel given."


_THINK_FILE = "think_flags.json"

_THINK_RE = re.compile(r"^/think(?:\s+(.*?))?\s*$", re.IGNORECASE)
_RESEARCH_RE = re.compile(r"^/research(?:\s+(.*?))?\s*$", re.IGNORECASE)

HELP_TEXT = (
    "/think — toggle maximum thinking for this conversation (🧠 button does the same).\n"
    "/think <goal> — run one goal with maximum thinking.\n"
    "/research — toggle deep-research mode for this conversation.\n"
    "/research <goal> — run one goal with deep research first, at maximum thinking.\n"
    "/web <question> — force a web answer for one question (🌐 button does the same).\n"
    "/mirror slack #channel — link this conversation to a Slack channel.\n"
    "/mirror slack off — remove the Slack link.\n"
    "/mirror webui <conversation-id> — from Slack, link a channel here.\n"
    "/mirror whatsapp on|off — mirror this conversation to WhatsApp.\n"
    "/mirror — show the current mirror status.\n"
    "/remember <fact> — save a fact to long-term memory.\n"
    "approve <id> / reject <id> — decide a pending approval card."
)

CLI_HELP_TEXT = (
    "/think — toggle maximum thinking for this REPL session.\n"
    "/think <goal> — run one goal with maximum thinking.\n"
    "/research — toggle deep-research mode for this REPL session.\n"
    "/research <goal> — run one goal with deep research first, at maximum thinking.\n"
    "/help — this list.\n"
    "/history — past goals this session.\n"
    "/clear — clear history.\n"
    "/remember <fact> — save a fact to long-term memory.\n"
    "/attach <path> — stage a file for the next goal.\n"
    "/staged — list staged files.\n"
    "/clear-staged — drop staged files.\n"
    "/quit — leave."
)


def _think_path(home: Path) -> Path:
    return Path(home) / _THINK_FILE


def load_think_flags(home: Path) -> dict:
    """Return the think-max flags, {} when the file is absent or broken."""
    try:
        data = json.loads(_think_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {}
    flags = data.get("think", {}) if isinstance(data, dict) else {}
    return {str(k): bool(v) for k, v in flags.items()
            if isinstance(k, str)} if isinstance(flags, dict) else {}


def _save_think_flags(home: Path, flags: dict) -> None:
    path = _think_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    # Read-merge: the same file also holds the research section,
    # and a think toggle must never wipe it (or vice versa).
    data["think"] = flags
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def think_enabled(home: Path, key: str) -> bool:
    """True when max thinking is on for a conversation key."""
    return bool(load_think_flags(home).get(str(key or ""), False))


def set_think(home: Path, key: str, enabled: bool) -> bool:
    """Set max thinking for a conversation key; returns the new state."""
    key = str(key or "")
    flags = load_think_flags(home)
    if enabled:
        flags[key] = True
    else:
        flags.pop(key, None)
    _save_think_flags(home, flags)
    return bool(enabled)


def toggle_think(home: Path, key: str) -> bool:
    """Flip max thinking for a conversation key; returns the new state."""
    return set_think(home, key, not think_enabled(home, key))


def parse_think_command(text: str) -> dict | None:
    """Parse a /think command; None when the text is not one.

    Bare ``/think`` toggles the sticky flag; ``/think <goal>``
    runs one goal with max thinking without changing the flag.
    """
    match = _THINK_RE.match(str(text or "").strip())
    if not match:
        return None
    rest = (match.group(1) or "").strip()
    if not rest:
        return {"action": "toggle"}
    return {"action": "once", "goal": rest}


def load_research_flags(home: Path) -> dict:
    """Return the research-mode flags, {} when absent or broken.

    Research flags share think_flags.json with the think flags
    (a second section in the same file), so one flags file holds
    both per-conversation run toggles.
    """
    try:
        data = json.loads(_think_path(home).read_text(encoding="utf-8"))
    except Exception:
        return {}
    flags = data.get("research", {}) if isinstance(data, dict) else {}
    return {str(k): bool(v) for k, v in flags.items()
            if isinstance(k, str)} if isinstance(flags, dict) else {}


def _save_research_flags(home: Path, flags: dict) -> None:
    path = _think_path(home)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["research"] = flags
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def research_enabled(home: Path, key: str) -> bool:
    """True when deep-research mode is on for a conversation key."""
    return bool(load_research_flags(home).get(str(key or ""), False))


def set_research(home: Path, key: str, enabled: bool) -> bool:
    """Set deep-research mode for a conversation key; returns the new state."""
    key = str(key or "")
    flags = load_research_flags(home)
    if enabled:
        flags[key] = True
    else:
        flags.pop(key, None)
    _save_research_flags(home, flags)
    return bool(enabled)


def toggle_research(home: Path, key: str) -> bool:
    """Flip deep-research mode for a conversation key; returns the new state."""
    return set_research(home, key, not research_enabled(home, key))


def parse_research_command(text: str) -> dict | None:
    """Parse a /research command; None when the text is not one.

    Bare ``/research`` toggles the sticky flag; ``/research
    <goal>`` runs one goal with deep research first, at maximum
    thinking, without changing the flag.
    """
    match = _RESEARCH_RE.match(str(text or "").strip())
    if not match:
        return None
    rest = (match.group(1) or "").strip()
    if not rest:
        return {"action": "toggle"}
    return {"action": "once", "goal": rest}
