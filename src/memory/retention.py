"""Pure retention and forgetting-curve policy for memory points."""

import math
from datetime import datetime, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_iso(value: str | None):
    if not value or value == "never":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def decay_score(access_count: int, last_accessed_iso: str | None, *,
                half_life_days: float = 21.0,
                access_day_count: int = 0) -> float:
    """Ebbinghaus-style decay score. Higher = more alive."""
    try:
        count = max(0, int(access_count or 0))
    except (TypeError, ValueError):
        return 0.0
    if count <= 0:
        return 0.0
    last = _parse_iso(last_accessed_iso)
    if last is None:
        return 0.0
    days = max(0.0, (datetime.now(timezone.utc) - last).total_seconds() / 86400.0)
    strength = 1.0 + math.log1p(min(count, 255))
    try:
        day_n = max(0, int(access_day_count or 0))
    except (TypeError, ValueError):
        day_n = 0
    effective = max(float(half_life_days), 1e-6) * (1.0 + 0.35 * math.log1p(day_n))
    return float(strength) * (0.5 ** (days / effective))


def is_grace_protected(created_at_iso: str | None, grace_days: int = 35) -> bool:
    created = _parse_iso(created_at_iso)
    if created is None:
        return False
    days = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
    return days < grace_days


def should_prune(payload: dict, *, half_life_days: float = 21.0,
                 threshold: float = 0.02, grace_days: int = 35) -> bool:
    """True when a point is a cleanup candidate, never for grace."""
    if is_grace_protected(payload.get("created_at"), grace_days):
        return False
    return decay_score(payload.get("access_count", 0),
                       payload.get("last_accessed"),
                       half_life_days=half_life_days,
                       access_day_count=payload.get("access_day_count", 0)) < threshold
