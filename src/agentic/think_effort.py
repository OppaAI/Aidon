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

import os

EFFORT_LEVELS = ("off", "low", "medium", "high")

_EFFORT_TABLE = {
    "interpret": "low",
    "research": "medium",
    "plan": "low",
    "execute": "medium",
    "verify": "low",
    "learn": "low",
    "respond": "off",
    # Web answers need a little synthesis to reconcile retrieved sources,
    # but do not need the full planning/reasoning budget of an agentic run.
    "webchat": "low",
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
    "webchat": {"temperature": 0.2, "top_p": 0.95},
    "execute": {"temperature": 0.2, "top_p": 0.95},
}

_DEFAULT_SAMPLING = {"temperature": 0.2, "top_p": 0.95}


def _env_effort_pin(phase: str) -> str | None:
    """Pinned effort from AIDON_EFFORT_<PHASE>, or None when unset/invalid.

    A pin is a user decision, not a suggestion: escalation never raises
    it (see resolve_effort), matching the .env.example documentation.
    """
    raw = os.environ.get(f"AIDON_EFFORT_{str(phase).upper()}")
    if raw is not None and raw.strip() in EFFORT_LEVELS:
        return raw.strip()
    return None


def effort_for(phase: str, overrides: dict | None = None) -> str:
    """Effort level for a phase; overrides win, unknown phases get low."""
    if overrides and phase in overrides and overrides[phase] in EFFORT_LEVELS:
        return overrides[phase]
    pin = _env_effort_pin(phase)
    if pin is not None:
        return pin
    return _EFFORT_TABLE.get(phase, "low")


def sampling_for(phase: str, *, escalate: bool = False,
                 creative: bool = False, think_max: bool = False) -> dict:
    """Sampling parameters for a phase with graceful effort degradation.

    Backends that expose only a thinking on/off switch receive ``think``.
    Backends that do not understand that hint simply ignore it and still
    receive the portable temperature/top-p settings. With think_max,
    every phase runs at the top of the ladder and the payload carries
    the model's own maximum reasoning effort (``reasoning_effort``):
    an explicit run-time toggle wins over .env effort pins, because
    it is the newer, more specific user decision.
    """
    level = "high" if think_max else resolve_effort(phase, escalate=escalate)
    result = dict(_SAMPLING_TABLE.get(phase, _DEFAULT_SAMPLING))
    if creative and str(phase).lower() in {"respond", "learn"}:
        # Open-ended writing benefits from a wider distribution than
        # operational answers, while planning and verification remain
        # conservative and machine-readable.
        result.update(temperature=1.0, top_p=0.98)
    # Preserve old per-phase .env overrides, e.g.
    # AIDON_TEMP_VERIFY=0.0 and AIDON_TOP_P_RESPOND=0.95.
    # AIDON_DYNAMIC_SAMPLING=0 makes sampling static: the built-in
    # table only, per-phase .env overrides ignored.
    dynamic = os.environ.get("AIDON_DYNAMIC_SAMPLING", "1").strip().lower() not in ("0", "false", "no", "off")
    if dynamic:
        temp = os.environ.get(f"AIDON_TEMP_{str(phase).upper()}")
        top_p = os.environ.get(f"AIDON_TOP_P_{str(phase).upper()}")
        try:
            if temp is not None and temp.strip():
                result["temperature"] = float(temp)
        except ValueError:
            pass
        try:
            if top_p is not None and top_p.strip():
                result["top_p"] = float(top_p)
        except ValueError:
            pass
    if str(phase).lower() == "verify":
        raw_seed = os.environ.get("AIDON_VERIFY_SEED")
        # Old default: deterministic verify calls use seed 42. An explicitly
        # empty value disables the seed; an invalid value is ignored.
        if raw_seed is None:
            result["seed"] = 42
        elif raw_seed.strip():
            try:
                result["seed"] = int(raw_seed)
            except ValueError:
                pass
    phases_raw = os.environ.get("AIDON_THINK_PHASES")
    if phases_raw is not None:
        phases = {part.strip().lower() for part in phases_raw.split(",")
                  if part.strip()}
        result["think"] = ("all" in phases or
                            str(phase).lower() in phases or
                            (escalate and level != "off"))
    else:
        # Native backends may only support a boolean thinking switch. All
        # positive effort levels degrade to that supported representation.
        result["think"] = think_max or level != "off"
    if think_max:
        # The model's own maximum: Meta's chat completions accept
        # reasoning_effort up to "xhigh" (its ladder tops above
        # Aidon's "high"); backends that do not know the field ignore
        # it, like the think hint above.
        result["think"] = True
        result["reasoning_effort"] = "xhigh"
    return result


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
        # A pinned level is never raised by escalation.
        if requested is None and _env_effort_pin(phase) is not None:
            return level
        index = EFFORT_LEVELS.index(level)
        level = EFFORT_LEVELS[min(index + 1, len(EFFORT_LEVELS) - 1)]
    return level
