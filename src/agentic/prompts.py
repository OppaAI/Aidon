"""Prompt templates, old Aidon prompts concept, rebuilt as a separate module.

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
