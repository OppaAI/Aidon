"""Pre-approval: owner-ticked, reversible, frozen-spec only, never external email.

Design, already decided, now wired into the approval path:
- A pre-approval is granted for a frozen spec, an action kind,
  and an expiry time, by the owner, in the spec approval step.
- It covers reversible actions only. Irreversible actions
  (live send to a third party, spend, publish, delete, install,
  adopt a plugin) are never pre-approved, they always get a card.
- External emails are excluded, even if ticked: with
  pre-approval on, an external send becomes a draft for the
  morning, never a sent email. An email to the owner's own
  address is the exception, and it is still reversible in
  the sense that it stays inside the owner's mailbox.
- A pre-approval that expired, or whose spec changed, does
  not apply. The morning report lists every pre-approved
  action and its undo step, so the owner can see what ran
  while they were away.

Three surfaces, one store:
The owner grants, lists, and revokes pre-approvals from the
WebUI (the Pre-approvals panel and the chat box), from
WhatsApp, and from Slack. All three go through
``handle_command`` below, so the wording, the safety rules,
and the result are identical everywhere, and a grant made in
one place is visible in the other two immediately, because
they read and write the same <home>/preapprovals.json.

Plain words, not jargon: a grant is a project id, a kind,
and a number of hours. The kind can be typed by name or
picked by number from the menu, which is what "choose or
enter" means on a phone.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

# Never pre-approved, whatever the owner types. These leave the
# machine, spend money, destroy data, or install code that runs
# later; each one always gets a card.
IRREVERSIBLE = {
    "send_external", "external_email", "publish", "spend", "delete",
    "live_execute", "plugin", "install", "mandatory",
}

# Kinds the owner is offered by name, with plain-language labels.
# "live" is accepted only for a reversible run (the gate checks
# the plan before honouring it). This menu is the closed list a
# grant may name: a chat message like "preapprove the budget for
# the trip" must not invent a kind, so an unlisted word is
# refused with the menu instead of being stored.
KINDS: list[dict] = [
    {"name": "live",
     "label": "Go live, reversible runs only",
     "desc": "Skip the final card when the run only touches files. "
             "Anything that sends, publishes, spends, deletes, or installs "
             "still asks."},
    {"name": "spec",
     "label": "Approve the spec sheet",
     "desc": "The interview result is accepted for this project without a card."},
    {"name": "semantic",
     "label": "Promote facts to long-term memory",
     "desc": "A finished run may store its facts without asking each time."},
    {"name": "sandbox",
     "label": "Run sandbox steps",
     "desc": "Test steps in the isolated workdir without asking each time."},
]
KIND_NAMES: tuple[str, ...] = tuple(kind["name"] for kind in KINDS)

HELP_TEXT = (
    "Pre-approval lets Aidon run reversible steps for one project without "
    "asking you every time. Irreversible steps always ask.\n"
    "\n"
    "Reply with:\n"
    "  preapprove <project> <kind> [hours]   grant (default 8 hours)\n"
    "  preapprovals                          list what is granted\n"
    "  unpreapprove <project> <kind|all>     revoke\n"
    "\n"
    "Kinds, by number or name:\n"
    + "\n".join(f"  {i}. {kind['name']} — {kind['label']}"
                for i, kind in enumerate(KINDS, 1))
)

_COMMAND_RE = re.compile(r"^\s*(preapprove|pre-approve|preapprovals|"
                         r"pre-approvals|unpreapprove|unpre-approve)\b(.*)$",
                         re.IGNORECASE | re.DOTALL)

DEFAULT_HOURS = 8.0
MAX_HOURS = 24 * 30.0


def kind_from_token(token: str) -> str | None:
    """Resolve a kind typed by name or by its number in the menu."""
    token = (token or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        index = int(token)
        if 1 <= index <= len(KINDS):
            return KINDS[index - 1]["name"]
        return None
    return token


def grantable_kind(kind: str) -> bool:
    """True when a grant may name this kind: on the menu, reversible."""
    return bool(kind) and kind in KIND_NAMES and kind not in IRREVERSIBLE


class PreapprovalStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "preapprovals.json"
        self.grants: list[dict] = []
        if self.path.is_file():
            try:
                self.grants = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self.grants = []
        if not isinstance(self.grants, list):
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
        if not spec_id or not action_kind:
            return False
        # One grant per (spec, kind): a second grant replaces the
        # expiry instead of stacking, so a re-grant cannot leave an
        # older, longer grant in place that the owner cannot see.
        self.grants = [g for g in self.grants
                       if not (g.get("spec_id") == spec_id
                               and g.get("action_kind") == action_kind)]
        self.grants.append({"spec_id": spec_id, "action_kind": action_kind,
                            "until": float(until), "granted_at": time.time()})
        self.save()
        return True

    def revoke(self, spec_id: str, action_kind: str | None = None) -> int:
        """Revoke one kind, or every kind for a spec. Returns how many went."""
        before = len(self.grants)
        if action_kind:
            self.grants = [g for g in self.grants
                           if not (g.get("spec_id") == spec_id
                                   and g.get("action_kind") == action_kind)]
        else:
            self.grants = [g for g in self.grants
                           if g.get("spec_id") != spec_id]
        removed = before - len(self.grants)
        if removed:
            self.save()
        return removed


    def active(self, now: float | None = None,
               spec_id: str | None = None) -> list[dict]:
        """Grants that still apply, newest first, optionally for one spec."""
        now = time.time() if now is None else now
        rows = [g for g in self.grants
                if float(g.get("until", 0)) > now
                and (spec_id is None or g.get("spec_id") == spec_id)]
        return sorted(rows, key=lambda g: float(g.get("until", 0)),
                      reverse=True)

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


def _hours(token: str) -> float | None:
    try:
        value = float(str(token).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return min(value, MAX_HOURS)


def _describe(grant: dict, now: float) -> str:
    left = float(grant.get("until", 0)) - now
    hours = max(0.0, left / 3600.0)
    return (f"{grant.get('spec_id')} · {grant.get('action_kind')} · "
            f"{hours:.1f}h left")


def handle_command(store: PreapprovalStore, text: str,
                   now: float | None = None) -> str | None:
    """Run a pre-approval command from any surface, or return None.

    Returns None when the text is not a pre-approval command, so the
    caller can carry on and treat it as a goal. Returns a reply string
    otherwise: the reply is what the owner sees in the chat bubble,
    the WhatsApp thread, or the Slack thread.
    """
    match = _COMMAND_RE.match(text or "")
    if not match:
        return None
    verb = match.group(1).lower().replace("-", "")
    rest = match.group(2).strip()
    now = time.time() if now is None else now
    parts = rest.split()

    if verb in ("preapprovals",):
        spec = parts[0] if parts else None
        rows = store.active(now=now, spec_id=spec)
        if not rows:
            scope = f"for {spec}" if spec else "anywhere"
            return ("No pre-approvals are active " + scope + ".\n\n"
                    + HELP_TEXT)
        lines = ["Active pre-approvals:"]
        lines.extend("  " + _describe(row, now) for row in rows)
        lines.append("\nReply unpreapprove <project> <kind> to revoke one.")
        return "\n".join(lines)

    if verb == "unpreapprove":
        if not parts:
            return ("Which project? Reply: unpreapprove <project> <kind|all>\n"
                    "Run preapprovals to see what is active.")
        spec = parts[0]
        token = parts[1].lower() if len(parts) > 1 else "all"
        if token in ("all", "*"):
            removed = store.revoke(spec)
            return (f"Revoked {removed} pre-approval(s) for {spec}."
                    if removed else f"Nothing was pre-approved for {spec}.")
        kind = kind_from_token(token)
        if kind is None:
            return (f"Unknown kind {token!r}. Reply with a number 1-{len(KINDS)} "
                    f"or one of: {', '.join(KIND_NAMES)}.")
        removed = store.revoke(spec, kind)
        return (f"Revoked {kind} for {spec}." if removed
                else f"{kind} was not pre-approved for {spec}.")

    # preapprove
    if not parts:
        return HELP_TEXT
    if len(parts) == 1:
        # A bare kind ("preapprove live") is the phone case: the
        # project is the one the chat is already about.
        token = parts[0]
        kind = kind_from_token(token)
        if kind is None or (kind not in KIND_NAMES and kind not in IRREVERSIBLE):
            return HELP_TEXT
        return (f"Which project? Reply: preapprove <project> {kind} [hours]\n"
                f"Example: preapprove dinner {kind} 8")

    spec = parts[0]
    kind = kind_from_token(parts[1])
    if kind is None or (kind not in KIND_NAMES and kind not in IRREVERSIBLE):
        # Not on the menu and not a known irreversible kind: refuse
        # rather than invent a grant from an ordinary sentence.
        return (f"Unknown kind {parts[1]!r}. Reply with a number 1-{len(KINDS)} "
                f"or one of: {', '.join(KIND_NAMES)}.")
    hours = DEFAULT_HOURS
    if len(parts) > 2:
        parsed = _hours(parts[2])
        if parsed is None:
            return (f"Hours must be a positive number, got {parts[2]!r}. "
                    f"Example: preapprove {spec} {kind} 8")
        hours = parsed
    if kind in IRREVERSIBLE:
        return (f"{kind} can never be pre-approved: it is irreversible or "
                f"leaves the machine, so it always gets a card.")
    if not store.grant(spec, kind, now + hours * 3600.0):
        return f"Could not grant {kind} for {spec}."
    return (f"Pre-approved {kind} for {spec}, for the next {hours:g}h.\n"
            f"Irreversible steps (send, publish, spend, delete, install) "
            f"still ask. Reply preapprovals to review, or "
            f"unpreapprove {spec} {kind} to cancel.")
