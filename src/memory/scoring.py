"""Deterministic memory eligibility scoring."""

from __future__ import annotations

import math
from datetime import datetime, timezone


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _age_days(value, now: datetime | None = None) -> float:
    try:
        created = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - created).total_seconds() / 86400.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def ltm_eligibility(payload: dict | None, *, now: datetime | None = None,
                    half_life_days: float = 30.0) -> float:
    """Return an explainable score in ``[0, 1]`` without writing or promoting."""
    data = payload or {}
    if data.get("pinned") is True:
        return 1.0
    try:
        importance = float(data.get("importance", data.get("salience", 0.0)))
    except (TypeError, ValueError):
        importance = 0.0
    try:
        valence = abs(float(data.get("valence", 0.0)))
    except (TypeError, ValueError):
        valence = 0.0
    try:
        access = math.log1p(max(0, int(data.get("access_count", 0)))) / math.log1p(8)
    except (TypeError, ValueError):
        access = 0.0
    salience = _clamp(max(importance, 0.65 * valence) + 0.20 * _clamp(access))
    if data.get("status") in {"done", "success", "successful"}:
        salience = _clamp(salience + 0.10)
    try:
        half_life = max(0.5, float(half_life_days))
    except (TypeError, ValueError):
        half_life = 30.0
    recency = 0.5 ** (_age_days(data.get("created_at"), now) / half_life)
    return round(_clamp(recency * salience), 6)
