"""Human direction: when to ask, how to get attention, and what is blocked.

This is the direction layer, not the approval layer. Approval decides
whether an action may proceed; direction decides whether a human should
be asked at all, and how to reach them when they should. It never picks
an option, never grants a permission, and never overrides a gate.

- ChoicePolicy routes a decision to "ask" or "decide" from stakes and
  confidence only. It returns the options untouched; picking one is the
  human's job (or the caller's, when the policy says "decide").
- AttentionNotifier fans a message out to configured channels (Slack,
  WebUI, CLI, or anything else the caller wires in as a callable).
  Channels are callables, not transports, so this module has no network
  code and no channel-specific logic: wiring is the caller's job.
- blocked_note is the structured form of a blocked report. Note: the
  existing declare_blocked tool in src/system/main.py reports a block
  as the string "[blocked] <reason>"; this helper returns the same
  information as a dict so callers that need the reason as data can
  read it without parsing a string. Wiring that replaces the tool
  should reconcile the two shapes, not silently mix them.
- The no-paid rule: a direction decision must never route to a paid
  provider without an explicit human decision. is_paid_provider is the
  check for that rule, not an enforcement hook — enforcement belongs
  to the caller that routes, which can ask the human first.
"""

from __future__ import annotations

# Providers that cost money to use. A direction decision must never
# route to one of these without an explicit human decision. Extend
# this set when a new paid provider is integrated; never remove an
# entry to make a routing decision pass.
PAID_PROVIDERS = frozenset({"bright_data"})

# Confidence below this threshold on a medium-stakes decision means
# the agent is not sure enough to decide alone.
_MEDIUM_CONFIDENCE_THRESHOLD = 0.7

_KNOWN_STAKES = ("low", "medium", "high")


def _normalise_stakes(stakes) -> str:
    """Unknown stakes are treated as high: fail towards asking."""
    try:
        value = str(stakes).strip().lower()
    except Exception:
        return "high"
    return value if value in _KNOWN_STAKES else "high"


def _normalise_confidence(confidence) -> float:
    """Coerce to float; an unreadable confidence is treated as 0."""
    try:
        return float(confidence)
    except (TypeError, ValueError):
        return 0.0


class ChoicePolicy:
    """Route a decision to "ask" or "decide" from stakes and confidence.

    - high stakes: always ask, whatever the confidence.
    - medium stakes: ask when confidence is below 0.7, decide otherwise.
    - low stakes: never ask.
    Unknown stakes are treated as high, so a new or misspelled stakes
    value fails towards asking, never towards deciding alone.
    """

    def should_ask(self, stakes, confidence) -> bool:
        stakes = _normalise_stakes(stakes)
        if stakes == "high":
            return True
        if stakes == "medium":
            return _normalise_confidence(confidence) < _MEDIUM_CONFIDENCE_THRESHOLD
        return False

    def decide(self, stakes, confidence, options):
        """Return {"action": "ask"|"decide", "options": options}.

        The options are returned untouched. This policy never picks an
        option itself: "ask" means a human picks, "decide" means the
        caller may proceed with its own choice.
        """
        action = "ask" if self.should_ask(stakes, confidence) else "decide"
        return {"action": action, "options": options}


class AttentionNotifier:
    """Fan a message out to configured channels, one failure at a time.

    channels maps a channel name to a callable(message) -> bool, or to
    None for a channel that is not configured. A callable returning
    False counts as a failed delivery, exactly like one that raises:
    in both cases the other channels are still attempted, and the
    failure is reported rather than raised.
    """

    def __init__(self, channels=None):
        self.channels = dict(channels or {})

    def notify(self, message, only=None):
        """Send message to each configured channel (or only the listed ones).

        Returns {"delivered": [...], "failed": [...], "skipped": [...]},
        where skipped covers channels that are unconfigured (None) and
        names in `only` that have no channel entry at all. Order within
        each list follows the channel dict's order.
        """
        if only is None:
            names = list(self.channels)
        else:
            names = list(only)
        delivered, failed, skipped = [], [], []
        for name in names:
            if name not in self.channels or self.channels[name] is None:
                skipped.append(name)
                continue
            try:
                ok = self.channels[name](message)
            except Exception:
                failed.append(name)
                continue
            if ok:
                delivered.append(name)
            else:
                failed.append(name)
        return {"delivered": delivered, "failed": failed, "skipped": skipped}


def blocked_note(reason) -> dict:
    """Structured form of a blocked report: {"blocked": True, "reason": ...}.

    This matches the information the existing declare_blocked tool
    reports, as a dict instead of its "[blocked] <reason>" string.
    """
    return {"blocked": True, "reason": str(reason)}


def is_paid_provider(name) -> bool:
    """True when name is a paid provider, case-insensitive, else False."""
    if not isinstance(name, str):
        return False
    return name.strip().lower() in PAID_PROVIDERS
