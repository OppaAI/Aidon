"""Per-phase thinking effort and sampling.

Thinking effort is a level, not a boolean: extended reasoning helps
prose judgment and hurts phases that must emit machine-parseable
JSON, so each phase has its own default. Sampling follows the same
shape: deterministic phases (verify) get a low temperature, open
phases (respond) get a higher one. Per-phase overrides always win
over the table. Escalation raises the level by one step, and the
caller passes escalate=True only when a pass was rejected by the
verifier on its merits — never for network, I/O, or tool failures,
where thinking harder cannot fix the problem.
"""

from __future__ import annotations

EFFORT_LEVELS = ("off", "low", "medium", "high")

_EFFORT_TABLE = {
    "interpret": "low",
    "research": "medium",
    "plan": "low",
    "execute": "medium",
    "verify": "low",
    "learn": "low",
    "respond": "off",
    "compact": "low",
}

_SAMPLING_TABLE = {
    "verify": {"temperature": 0.0, "top_p": 0.9},
    "interpret": {"temperature": 0.1, "top_p": 0.95},
    "research": {"temperature": 0.2, "top_p": 0.95},
    "plan": {"temperature": 0.2, "top_p": 0.95},
    "learn": {"temperature": 0.3, "top_p": 0.95},
    "compact": {"temperature": 0.1, "top_p": 0.95},
    "respond": {"temperature": 0.8, "top_p": 0.95},
    "execute": {"temperature": 0.2, "top_p": 0.95},
}

_DEFAULT_SAMPLING = {"temperature": 0.2, "top_p": 0.95}


def effort_for(phase: str, overrides: dict | None = None) -> str:
    """Effort level for a phase; overrides win, unknown phases get low."""
    if overrides and phase in overrides and overrides[phase] in EFFORT_LEVELS:
        return overrides[phase]
    return _EFFORT_TABLE.get(phase, "low")


def sampling_for(phase: str) -> dict:
    """Sampling parameters for a phase; unknown phases get the default."""
    return dict(_SAMPLING_TABLE.get(phase, _DEFAULT_SAMPLING))


def resolve_effort(phase: str, requested: str | None = None,
                   escalate: bool = False, overrides: dict | None = None) -> str:
    """Resolve a phase's effort, optionally escalating one level.

    A requested level wins over the table. Escalation moves one
    step up the ladder (off -> low -> medium -> high, high stays
    high). Callers must only set escalate=True for verifier
    rejections on the merits.
    """
    if requested is not None:
        if requested not in EFFORT_LEVELS:
            raise ValueError(f"unknown effort level {requested!r}")
        level = requested
    else:
        level = effort_for(phase, overrides)
    if escalate:
        index = EFFORT_LEVELS.index(level)
        level = EFFORT_LEVELS[min(index + 1, len(EFFORT_LEVELS) - 1)]
    return level
