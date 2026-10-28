"""Explicit requests to deliver a completed WebUI run to another channel."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ResultDelivery:
    channel: str


# Keep this deliberately narrow: a delivery request must name both a result
# and a supported channel. Ordinary task text must remain ordinary task text.
_DIRECTIVE_RE = re.compile(
    r"(?:,?\s*(?:and\s+)?(?:when\s+finished|when\s+done|on\s+completion|"
    r"after\s+(?:it|the\s+task)\s+finishes)\s*)?"
    r"(?:please\s+)?(?:send|message|notify)\s+(?:me\s+)?(?:the\s+)?"
    r"(?:final\s+)?result\s+(?:to\s+me\s+)?(?:to|via|on)\s+"
    r"(whatsapp|slack)\b\s*[,.;:!?]*$",
    re.IGNORECASE,
)


def extract_result_delivery(text: str) -> tuple[str, ResultDelivery | None]:
    """Remove an explicit final-result delivery suffix from a goal."""
    value = str(text or "").strip()
    match = _DIRECTIVE_RE.search(value)
    if not match:
        return value, None
    channel = match.group(1).lower()
    goal = value[:match.start()].rstrip(" ,.;:!?\n")
    goal = re.sub(
        r"\s*(?:and\s+)?(?:when\s+finished|when\s+done|on\s+completion|"
        r"after\s+(?:it|the\s+task)\s+finishes)\s*$", "", goal,
        flags=re.IGNORECASE).rstrip(" ,.;:!?\n")
    if goal.lower().endswith(" and"):
        goal = goal[:-4].rstrip()
    if not goal:
        return value, None
    return goal, ResultDelivery(channel)
