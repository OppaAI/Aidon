"""Task patterns, old Aidon patterns concept, rebuilt.

A pattern is a named reusable plan shape: keywords, steps, and deliverables. match_pattern(goal)
returns the first pattern whose keywords appear in the goal, or None, so a goal can be planned
from a pattern without a model call, and the pattern can be adapted by the DAG adapter.
Patterns are data, not model output, and adding one is adding a dict, not retraining anything.
"""

from __future__ import annotations

PATTERNS = [
    {"name": "write_file", "keywords": ["write", "create", "make"], "steps": [{"tool": "write", "deliverable": "output"}], "deliverables": ["output"]},
    {"name": "research_write", "keywords": ["research", "report", "summary"], "steps": [{"tool": "write", "deliverable": "report"}], "deliverables": ["report"]},
    {"name": "plan_only", "keywords": ["plan", "outline"], "steps": [{"tool": "write", "deliverable": "plan"}], "deliverables": ["plan"]},
]


def match_pattern(goal: str, patterns: list[dict] | None = None) -> dict | None:
    text = (goal or "").lower()
    for pattern in patterns or PATTERNS:
        if any(keyword in text for keyword in pattern.get("keywords", [])):
            return pattern
    return None
