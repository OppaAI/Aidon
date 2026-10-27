"""Graduated approvals: learn how often to ask, never what is forbidden.

Ported from old Aidon autonomy.py, same semantics. The AutonomyPolicy
keeps per-action-class TrustRecords: consecutive EXPLICIT human
approvals graduate low/medium-stakes classes to act/act_and_notify.
The boundary is deliberate and one-way:

    the learner may change HOW OFTEN Aidon asks, never WHAT it may do

High-stakes classes never graduate. New classes default to HIGH — a new
action class must opt in to autonomy, never inherit it by omission. Only
explicit approvals count (silence, nudges, and timeouts are not consent).
Trust decays (~14 days). Refusal resets the streak. Plain JSON counters
under the aidon home dir, with forget(). No model, no hidden state.

Stakes per approval kind: "skill" is medium (trusting a validated,
self-tested skill is the one routine approval worth graduating).
Privileged skills travel under their OWN class ("skill_privileged",
high): their approvals must not build the medium class's streak, and
the high tier never graduates regardless.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

STAKES_LOW = "low"
STAKES_MEDIUM = "medium"
STAKES_HIGH = "high"
_STAKES_ORDER = {STAKES_LOW: 0, STAKES_MEDIUM: 1, STAKES_HIGH: 2}

ACT = "act"
ACT_NOTIFY = "act_and_notify"
ASK = "ask"

ACTION_STAKES: dict[str, str] = {
    "skill": STAKES_MEDIUM,
    "skill_privileged": STAKES_HIGH,
    "plugin": STAKES_MEDIUM,
    "publish": STAKES_MEDIUM,
}


@dataclass
class TrustRecord:
    """Consecutive explicit approvals for one (user, action class) pair."""

    action_class: str
    stakes: str = STAKES_MEDIUM
    approvals: int = 0
    refusals: int = 0
    granted_at: float = 0.0
    revoked: bool = False
    history: list[dict] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "TrustRecord":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class AutonomyPolicy:
    """Per-user learned trust, capped by stakes, decayed over time."""

    def __init__(
        self,
        user_id: str = "owner",
        *,
        threshold: int = 5,
        decay_seconds: float = 14 * 24 * 3600.0,
        notify_threshold: int = 2,
        max_history: int = 50,
        path: Path | None = None,
        clock=time.time,
    ) -> None:
        self._user_id = user_id
        self._threshold = max(1, int(threshold))
        self._decay = float(decay_seconds)
        self._notify_threshold = max(1, int(notify_threshold))
        self._max_history = max(1, int(max_history))
        self._clock = clock
        self._records: dict[str, TrustRecord] = {}
        self._lock = threading.Lock()
        self._path = path
        self.audit: list[dict] = []
        if path is not None and path.exists():
            self._load(path)

    # ── policy ──
    def stakes_for(self, action_class: str) -> str:
        """Stakes tier for an action class. Unseen classes default to HIGH."""
        rec = self._records.get(action_class)
        return rec.stakes if rec else ACTION_STAKES.get(action_class, STAKES_HIGH)

    def permission(self, action_class: str, *,
                   stakes: str | None = None) -> dict:
        """Current verdict for acting on `action_class` without asking."""
        with self._lock:
            rec = self._records.get(action_class)
            tier = (stakes or (rec.stakes if rec else ACTION_STAKES.get(action_class, STAKES_HIGH))).lower()
            if tier not in _STAKES_ORDER:
                tier = STAKES_HIGH
            if tier == STAKES_HIGH:
                return self._verdict(ASK, action_class,
                                     "high-stakes class never graduates")
            if tier == STAKES_LOW:
                return self._verdict(ACT, action_class,
                                     "low stakes, no approval history needed")
            if rec is None:
                return self._verdict(ASK, action_class, "no approval history")
            if rec.revoked:
                return self._verdict(ASK, action_class, "permission revoked")
            self._apply_decay(rec)
            if rec.approvals >= self._threshold:
                return self._verdict(
                    ACT_NOTIFY if tier == STAKES_MEDIUM else ACT,
                    action_class,
                    f"standing permission after {rec.approvals} approvals ({tier})",
                )
            if rec.approvals >= self._notify_threshold:
                return self._verdict(
                    ACT_NOTIFY, action_class,
                    f"partial trust ({rec.approvals}/{self._threshold})")
            return self._verdict(ASK, action_class,
                                 f"only {rec.approvals} approvals so far")

    def approvals(self, action_class: str) -> int:
        """Consecutive approvals after decay, for threshold checks."""
        with self._lock:
            rec = self._records.get(action_class)
            if rec is None:
                return 0
            self._apply_decay(rec)
            return rec.approvals

    # ── learning ──
    def record_approval(self, action_class: str, *,
                        stakes: str | None = None) -> TrustRecord:
        """An explicit approval. Only this method increases trust."""
        with self._lock:
            rec = self._ensure(action_class, stakes)
            self._apply_decay(rec)
            rec.approvals += 1
            rec.refusals = 0
            rec.granted_at = self._clock()
            rec.revoked = False
            rec.history.append({"t": rec.granted_at, "event": "approved"})
            self._trim(rec)
            self.audit.append({"action_class": action_class, "event": "approved",
                               "approvals": rec.approvals})
            self._save()
            return rec

    def record_refusal(self, action_class: str, *,
                       stakes: str | None = None) -> TrustRecord:
        """An explicit refusal. Resets the streak — trust is consecutive."""
        with self._lock:
            rec = self._ensure(action_class, stakes)
            rec.approvals = 0
            rec.refusals += 1
            rec.revoked = True
            rec.history.append({"t": self._clock(), "event": "refused"})
            self._trim(rec)
            self.audit.append({"action_class": action_class, "event": "refused",
                               "refusals": rec.refusals})
            self._save()
            return rec

    def revoke(self, action_class: str) -> None:
        with self._lock:
            rec = self._records.get(action_class)
            if rec is not None:
                rec.approvals = 0
                rec.revoked = True
                rec.history.append({"t": self._clock(), "event": "revoked"})
                self._trim(rec)
                self._save()

    def forget(self, action_class: str) -> None:
        """Erase a class entirely (panic button / clean slate)."""
        with self._lock:
            self._records.pop(action_class, None)
            self.audit.append({"action_class": action_class,
                               "event": "forgotten"})
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "user_id": self._user_id,
                "threshold": self._threshold,
                "classes": {
                    k: {"stakes": v.stakes, "approvals": v.approvals,
                        "refusals": v.refusals, "revoked": v.revoked}
                    for k, v in sorted(self._records.items())
                },
            }

    # ── internals ──
    def _ensure(self, action_class: str, stakes: str | None) -> TrustRecord:
        rec = self._records.get(action_class)
        if rec is None:
            tier = (stakes or ACTION_STAKES.get(action_class, STAKES_HIGH)).lower()
            rec = TrustRecord(
                action_class=action_class,
                stakes=tier if tier in _STAKES_ORDER else STAKES_HIGH,
            )
            self._records[action_class] = rec
        elif stakes:
            tier = stakes.lower()
            if tier in _STAKES_ORDER:
                rec.stakes = tier
        return rec

    def _apply_decay(self, rec: TrustRecord) -> None:
        if not rec.granted_at or self._decay <= 0:
            return
        if self._clock() - rec.granted_at > self._decay:
            if rec.approvals:
                log.debug("autonomy decay: %s lost standing permission",
                          rec.action_class)
            rec.approvals = 0

    def _trim(self, rec: TrustRecord) -> None:
        if len(rec.history) > self._max_history:
            rec.history = rec.history[-self._max_history:]

    def _verdict(self, action: str, action_class: str, why: str) -> dict:
        entry = {"action_class": action_class, "action": action, "why": why}
        self.audit.append(entry)
        return entry

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({k: v.to_json() for k, v in self._records.items()},
                           indent=1),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("autonomy persist failed: %s", e)

    def _load(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, dict):
                        self._records[k] = TrustRecord.from_json(
                            {**v, "action_class": k})
        except Exception as e:  # corrupt file must not block startup
            log.warning("autonomy load failed (%s); starting empty",
                        type(e).__name__)


def policy_for(home: Path | str, *, threshold: int = 5,
               decay_days: float = 14.0, **kwargs) -> AutonomyPolicy:
    """Build the policy persisted at <home>/autonomy.json."""
    path = Path(home) / "autonomy.json"
    return AutonomyPolicy(path=path, threshold=threshold,
                          decay_seconds=decay_days * 24 * 3600.0, **kwargs)


__all__ = [
    "ACT", "ACT_NOTIFY", "ASK",
    "ACTION_STAKES",
    "AutonomyPolicy", "TrustRecord",
    "STAKES_HIGH", "STAKES_LOW", "STAKES_MEDIUM",
    "policy_for",
]
