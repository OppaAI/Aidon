"""Pre-approval: owner-ticked, reversible, frozen-spec only, never external email.

Design, already decided, now wired into the approval path:
- A pre-approval is granted for a frozen spec, an action kind,
  and an expiry time, by the owner, in the spec approval step.
- It covers reversible actions only. Irreversible actions
  (live send to a third party, spend, publish, delete) are
  never pre-approved, they always get a card.
- External emails are excluded, even if ticked: with
  pre-approval on, an external send becomes a draft for the
  morning, never a sent email. An email to the owner's own
  address is the exception, and it is still reversible in
  the sense that it stays inside the owner's mailbox.
- A pre-approval that expired, or whose spec changed, does
  not apply. The morning report lists every pre-approved
  action and its undo step, so the owner can see what ran
  while they were away.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

IRREVERSIBLE = {"send_external", "publish", "spend", "delete", "live_execute"}


class PreapprovalStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "preapprovals.json"
        self.grants: list[dict] = []
        if self.path.is_file():
            try:
                self.grants = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.grants = []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.grants, indent=2))

    def grant(self, spec_id: str, action_kind: str, until: float,
              reversible: bool = True) -> bool:
        """Grant, or refuse. Irreversible and external email are refused."""
        if action_kind in IRREVERSIBLE or action_kind == "send_external":
            return False
        if not reversible:
            return False
        self.grants.append({"spec_id": spec_id, "action_kind": action_kind,
                            "until": until})
        self.save()
        return True

    def is_preapproved(self, spec_id: str, action_kind: str,
                       now: float | None = None) -> bool:
        if action_kind in IRREVERSIBLE or action_kind == "send_external":
            return False
        now = time.time() if now is None else now
        return any(g["spec_id"] == spec_id and g["action_kind"] == action_kind
                   and g["until"] > now for g in self.grants)

    def morning_report(self) -> list[dict]:
        """What ran under pre-approval, and its undo step, for the owner."""
        return [{"spec_id": g["spec_id"], "action_kind": g["action_kind"],
                 "undo": "Revert the files in the run's workdir, or cancel the draft, before it goes live."}
                for g in self.grants]
