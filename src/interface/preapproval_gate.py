"""Pre-approval live gate wrapper: a ticked, reversible grant skips the card, nothing else does.

Pre-approval is owner-ticked, reversible, frozen-spec only.
It is not a blanket auto-approve. The wrapper enforces that
at the gate, so a live run cannot accidentally treat a
grant as permission for something the owner never ticked.

Rules, in order, each checked before the real gate is asked:
- Irreversible kinds always ask the real gate. The set
  comes from the store's IRREVERSIBLE attribute if it has
  one, otherwise from the preapproval module, otherwise
  from the built-in set: send_external, external_email,
  publish, spend, delete, live_execute, plugin, install,
  mandatory.
- External email always asks the real gate, even if it
  was somehow ticked. Kinds "external_email" and
  "send_external" are excluded, full stop. An external
  send under pre-approval becomes a draft for the
  morning, never a sent email.
- "live" is honoured only when the caller says the run is
  reversible (``reversible=True``): a plan that sends,
  publishes, spends, deletes, or installs is not reversible
  and still gets its card. A grant for "live" is therefore
  never a licence to do something irreversible.
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

_DEFAULT_IRREVERSIBLE = {
    "send_external", "external_email", "publish", "spend", "delete",
    "live_execute", "plugin", "install", "mandatory",
}
_EXTERNAL_KINDS = {"external_email", "send_external"}


class PreapprovalGate:
    """Wrap a real gate with a pre-approval check. The real gate is never bypassed for irreversible or external kinds."""

    def __init__(self, gate, store, spec_id: str):
        self.gate = gate
        self.store = store
        self.spec_id = spec_id

    def __getattr__(self, name):
        # Timeouts, policy, and anything else the wrapped gate exposes
        # stay reachable, so wrapping never changes how a caller
        # configures the gate.
        return getattr(self.gate, name)

    def _irreversible(self) -> set:
        store_set = getattr(self.store, "IRREVERSIBLE", None)
        combined = set(_DEFAULT_IRREVERSIBLE) | set(_MODULE_IRREVERSIBLE or set())
        if store_set:
            combined |= set(store_set)
        return combined

    def ask(self, kind: str, title: str, body: str, **kwargs) -> Decision:
        """Return an approved decision for a pre-approved reversible kind, else ask the real gate."""
        reversible = bool(kwargs.pop("reversible", False))
        if kind in _EXTERNAL_KINDS or kind in self._irreversible():
            return self.gate.ask(kind, title, body, **kwargs)
        if kind == "live" and not reversible:
            # A live card is the one decision that must stay with the
            # owner unless the plan is reversible, which the caller
            # states explicitly. Silence is not reversible.
            return self.gate.ask(kind, title, body, **kwargs)
        try:
            preapproved = self.store.is_preapproved(self.spec_id, kind, time.time())
        except Exception:
            preapproved = False
        if preapproved:
            return Decision("approved",
                            reason=f"pre-approved {kind} for {self.spec_id}")
        return self.gate.ask(kind, title, body, **kwargs)
