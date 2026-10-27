"""Pre-approval live gate wrapper: a ticked, reversible grant skips the card, nothing else does.

Pre-approval is owner-ticked, reversible, frozen-spec only.
It is not a blanket auto-approve. The wrapper enforces that
at the gate, so a live run cannot accidentally treat a
grant as permission for something the owner never ticked.

Rules, in order, each checked before the real gate is asked:
- Irreversible kinds always ask the real gate. The set
  comes from the store's IRREVERSIBLE attribute if it has
  one, otherwise from the preapproval module, otherwise
  from the built-in set: send_external, publish, spend,
  delete, live_execute.
- External email always asks the real gate, even if it
  was somehow ticked. Kinds "external_email" and
  "send_external" are excluded, full stop. An external
  send under pre-approval becomes a draft for the
  morning, never a sent email.
- If the store says this spec and kind are pre-approved
  right now, return an approved decision without asking
  the real gate, so an away owner is not woken for a
  reversible step they already ticked.
- Otherwise, ask the real gate and return its decision
  unchanged, including rejected and edited.

The approved decision reuses Decision from the approval
module, so callers see the same verdict shape whether the
approval came from a card or from a grant. The store's
is_preapproved is called as is_preapproved(spec_id, kind,
now), with now as a float timestamp, matching both the
real PreapprovalStore, whose now argument is optional,
and a fake store that requires it.
"""

from __future__ import annotations

import time

from .approval import Decision

try:
    from .preapproval import IRREVERSIBLE as _MODULE_IRREVERSIBLE
except Exception:
    _MODULE_IRREVERSIBLE = set()

_DEFAULT_IRREVERSIBLE = {"send_external", "publish", "spend", "delete", "live_execute"}
_EXTERNAL_KINDS = {"external_email", "send_external"}


class PreapprovalGate:
    """Wrap a real gate with a pre-approval check. The real gate is never bypassed for irreversible or external kinds."""

    def __init__(self, gate, store, spec_id: str):
        self.gate = gate
        self.store = store
        self.spec_id = spec_id

    def _irreversible(self) -> set:
        store_set = getattr(self.store, "IRREVERSIBLE", None)
        combined = set(_DEFAULT_IRREVERSIBLE) | set(_MODULE_IRREVERSIBLE or set())
        if store_set:
            combined |= set(store_set)
        return combined

    def ask(self, kind: str, title: str, body: str, **kwargs) -> Decision:
        """Return an approved decision for a pre-approved reversible kind, else ask the real gate."""
        if kind in _EXTERNAL_KINDS or kind in self._irreversible():
            return self.gate.ask(kind, title, body, **kwargs)
        try:
            preapproved = self.store.is_preapproved(self.spec_id, kind, time.time())
        except Exception:
            preapproved = False
        if preapproved:
            return Decision("approved")
        return self.gate.ask(kind, title, body, **kwargs)
