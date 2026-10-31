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


def current_time_line() -> str:
    """Return the configured local clock for prompts, with timezone name."""
    tz_name = os.environ.get("AIDON_TIMEZONE", "America/Vancouver")
    try:
        now = datetime.now(ZoneInfo(tz_name)) if ZoneInfo else datetime.now()
    except Exception:
        now = datetime.now()
    return f"Current local time: {now.strftime('%Y-%m-%d %H:%M %Z')} ({tz_name})."


def runtime_model_inventory() -> str:
    """Return the configured, non-secret model inventory for the agent.

    Model names are configuration facts, not eligibility evidence. Keeping
    this block in the runtime prompt lets Aidon compare its actual stack with
    a requirement it finds in official rules without exposing endpoints or
    credentials.
    """
    def env(name: str, default: str) -> str:
        value = os.environ.get(name, "").strip()
        return value or default

    backend = env("AIDON_BACKEND", "auto/inferred")
    main = env("AIDON_MODEL", "muse-spark")
    fast = env("AIDON_FAST_MODEL", "same as main")
    verifier = env("AIDON_VERIFIER_MODEL", "same as main")
    return (
        "# Runtime model inventory\n"
        f"{current_time_line()}\n"
        "These are the configured model and service identifiers for this run; "
        "they are not proof of current availability or hackathon eligibility.\n"
        f"- Agent model: {main}\n"
        f"- Fast model: {fast}\n"
        f"- Verifier model: {verifier}\n"
        f"- Model backend: {backend}\n"
        f"- Memory embedding model: {env('AIDON_EMBED_MODEL', 'google/embeddinggemma-2')}\n"
        f"- Image model: {env('AIDON_IMAGE_MODEL', 'muse-image-1.0')}\n"
        f"- Segmentation model: {env('AIDON_SAM_MODEL', 'meta-sam')}\n"
        "- Speech-to-text model: muse-voice-transcribe-1.0\n"
        "- Text-to-speech model: Fish Audio s2.1-pro-free\n"
        f"- Web search provider: {env('AIDON_SEARCH_BACKEND', 'tavily')}\n"
        "There is no separately configured code model unless one of the model "
        "tiers above is explicitly used for code. When checking a competition "
        "or integration requirement, browse the official rules, list the exact "
        "required models/APIs, and compare them with this inventory. Do not "
        "claim qualification from a brand resemblance or from this inventory alone."
    )


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
    parts = [SYSTEM_LINE]
    parts.append(current_time_line())
    if phase:
        parts.append(f"You are in the {phase} phase.")
    user_text = user_text or _read_doc("USER.md")
    if user_text:
        parts.append("\n# The user\n" + user_text)
    return "\n".join(parts)


def build_identity_prompt(phase: str = "", user_text: str = "",
                          persona: str = "") -> str:
    """Compact runtime identity: Aidon's persona plus the user profile,
    plus the manual sections every phase decides by: the loop, the
    plugin instructions, the capability ladder, and the hard rules.
    The full manual stays available via build_system_prompt; these
    sections are the ones a phase needs without paying for all of
    it on every call. When the manual is absent the sections are
    simply missing, never faked."""
    parts = [SYSTEM_LINE]
    if phase:
        parts.append(f"You are in the {phase} phase.")
    parts.append(runtime_model_inventory())
    persona = persona or _read_doc("SOUL.md")
    if persona:
        parts.append("\n# Persona\n" + persona)
    manual = _read_doc("AGENTS.md")
    if manual:
        for heading in ("The loop", "Writing tool plugins",
                        "Capability ladder", "Hard rules"):
            section = _section(manual, heading)
            if section:
                parts.append("\n" + section.rstrip())
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
