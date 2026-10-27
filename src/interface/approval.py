"""Approval gate: the human decides before any live action.

The gate has one rule: a decision is a card, a conversation is
chat. Interview questions are chat turns, not cards, and they
have no clock. Decisions — approve, edit, or reject a plan, a
live execution, a third-party send, a spend — are cards, and they
fail closed on timeout: no answer in time means rejected, never
approved by default.

An external agent can submit a goal and read a run, but it cannot
approve on the owner's behalf. Approval belongs to the human
owner, via the CLI, the WebUI, or a messaging surface, or to a
pre-approved reversible workflow that the owner explicitly ticked
in the spec. External emails are excluded from pre-approval:
with pre-approval on, an external send becomes a draft for the
morning, never a sent email.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass
class Decision:
    verdict: str  # approved | rejected | edited
    edited_text: str = ""


class ApprovalGate:
    """A gate that waits for a human decision, or fails closed.

    In the CLI skeleton, the decision comes from a callable the
    caller supplies (in production, the WebUI card or a messaging
    reply). In tests, the callable is a fake that returns a fixed
    decision immediately, so tests never block on a human.
    """

    def __init__(self, decide, timeout_seconds: int = 180):
        self._decide = decide
        self.timeout_seconds = timeout_seconds

    def ask(self, kind: str, title: str, body: str) -> Decision:
        """Ask for a decision. kind == 'question' is not a decision.

        Questions are conversation turns: they are answered in
        chat, not through this gate, and they have no clock.
        Callers must route questions to chat before calling ask.
        """
        if kind == "question":
            raise ValueError("questions are chat turns, not approval cards")
        result: list[Decision] = []
        thread = threading.Thread(
            target=lambda: result.append(self._decide(kind, title, body)),
            daemon=True)
        thread.start()
        thread.join(timeout=self.timeout_seconds)
        if not result:
            return Decision("rejected")
        verdict = result[0].verdict
        if verdict not in ("approved", "rejected", "edited"):
            return Decision("rejected")
        return result[0]
