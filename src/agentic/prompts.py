"""Prompt templates, Aidon prompts concept, rebuilt as a separate module.

Prompts are data, not code scattered through the loop: a named template with {placeholders},
rendered by code, with a hard error on a missing variable, never a silently empty placeholder.
This is the module to edit when a prompt changes, instead of editing the loop, the verifier,
or the interview code directly.
"""

from __future__ import annotations

TEMPLATES = {
    "interpret": "Interpret this goal, in one paragraph, with no extra tasks: {goal}",
    "verify": "Check these criteria against these files, list each as pass or fail: {criteria} files: {files}",
    "adapt": "Adapt this plan for this new goal, change only what must change: {plan} new goal: {goal}",
    "summarise": "Summarise this conversation in three sentences, no new facts: {conversation}",
}


def render_prompt(name: str, variables: dict) -> str:
    if name not in TEMPLATES:
        raise KeyError(f"unknown prompt template: {name}")
    template = TEMPLATES[name]
    missing = [key for key in _placeholders(template) if key not in variables]
    if missing:
        raise ValueError(f"prompt {name} missing variables: {', '.join(missing)}")
    return template.format(**variables)


def _placeholders(template: str) -> list[str]:
    import re
    return re.findall(r"\{(\w+)\}", template)


# -- system / digest / minimal prompts and context blocks --------------------

import os
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - very old Python
    ZoneInfo = None

SYSTEM_LINE = ("You are Aidon, an autonomous agent that learns from the web"
               " and from its own experience.")


def _read_doc(name: str) -> str:
    """Read a repository doc when present, '' when absent."""
    candidates = [os.path.join(os.getcwd(), name),
                  os.path.join(os.getcwd(), "docs", name)]
    try:
        repo_root = os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))))
        candidates.extend((os.path.join(repo_root, name),
                           os.path.join(repo_root, "docs", name)))
    except Exception:
        pass
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            continue
    return ""


def build_system_prompt(persona: str = "", manual: str = "",
                        user_text: str = "") -> str:
    """The full prompt: identity line, persona, operating manual,
    and user identity, each only when present."""
    parts = [SYSTEM_LINE]
    persona = persona or _read_doc("SOUL.md")
    if persona:
        parts.append("\n# Persona\n" + persona)
    manual = manual or _read_doc("AGENTS.md")
    if manual:
        parts.append("\n# Operating manual\n" + manual)
    user_text = user_text or _read_doc("USER.md")
    if user_text:
        parts.append("\n# The user\n" + user_text)
    return "\n".join(parts)


def _section(text: str, heading: str, stop: str = "\n## ") -> str:
    """One '## heading' section verbatim, '' when absent."""
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    end = text.find(stop, start + len(marker))
    return text[start:] if end < 0 else text[start:end]


def build_digest_prompt(manual: str = "", user_text: str = "") -> str:
    """Mid-weight prompt for judgment phases: the identity line plus
    the manual sections those phases decide by, plus the user."""
    parts = [SYSTEM_LINE]
    manual = manual or _read_doc("AGENTS.md")
    if manual:
        for heading, stop in (("Capability ladder", "\n## "),
                              ("Hard rules", "\n## ")):
            section = _section(manual, heading, stop)
            if section:
                parts.append("\n" + section.rstrip())
    user_text = user_text or _read_doc("USER.md")
    if user_text:
        parts.append("\n# The user\n" + user_text)
    return "\n".join(parts)


def build_minimal_prompt(phase: str = "", user_text: str = "") -> str:
    """Slim prompt for high-frequency rounds: identity line, the
    current time and phase, and the user — no persona, no manual."""
    tz = os.environ.get("AIDON_TIMEZONE", "America/Vancouver")
    try:
        now = datetime.now(ZoneInfo(tz)) if ZoneInfo else datetime.now()
    except Exception:
        now = datetime.now()
    parts = [SYSTEM_LINE]
    parts.append(f"Current time: {now.strftime('%Y-%m-%d %H:%M %Z')}.")
    if phase:
        parts.append(f"You are in the {phase} phase.")
    user_text = user_text or _read_doc("USER.md")
    if user_text:
        parts.append("\n# The user\n" + user_text)
    return "\n".join(parts)


def build_identity_prompt(phase: str = "", user_text: str = "",
                          persona: str = "") -> str:
    """Compact runtime identity: Aidon's persona plus the user profile."""
    parts = [SYSTEM_LINE]
    if phase:
        parts.append(f"You are in the {phase} phase.")
    persona = persona or _read_doc("SOUL.md")
    if persona:
        parts.append("\n# Persona\n" + persona)
    user_text = user_text or _read_doc("USER.md")
    if user_text:
        parts.append("\n# The user\n" + user_text)
    return "\n".join(parts)


def guidance_block(guidance: str) -> str:
    """User guidance as a binding block, or '' when there is none."""
    if not (guidance or "").strip():
        return ""
    return ("User guidance — binding, follow it and fill gaps, do not"
            f" improvise past it:\n{guidance.strip()}\n")


def context_block(context: str) -> str:
    if not (context or "").strip():
        return ""
    return f"Context:\n{context.strip()}\n"


def workspace_block(workspace: str) -> str:
    if not (workspace or "").strip():
        return ""
    return f"Workspace:\n{workspace.strip()}\n"


def capability_block(text: str) -> str:
    if not (text or "").strip():
        return ""
    return f"Available capabilities:\n{text.strip()}\n"
