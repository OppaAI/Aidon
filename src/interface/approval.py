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

Graduated autonomy (optional):
When an AutonomyPolicy is attached, the gate may short-circuit
medium/low-stakes action classes after repeated *explicit*
approvals. High-stakes classes never graduate. The policy may
change HOW OFTEN Aidon asks; it never expands WHAT is permitted.
Silence, nudges, and timeouts are not consent and do not build
trust. Explicit refusals reset the streak.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .autonomy import AutonomyPolicy


@dataclass
class Decision:
    verdict: str  # approved | rejected | edited | auto_approved
    edited_text: str = ""
    reason: str = ""  # why auto-approved / rejected by policy, if any


class ApprovalGate:
    """A gate that waits for a human decision, or fails closed.

    In the CLI skeleton, the decision comes from a callable the
    caller supplies (in production, the WebUI card or a messaging
    reply). In tests, the callable is a fake that returns a fixed
    decision immediately, so tests never block on a human.

    When ``policy`` is set, callers may pass ``action_class`` to
    ``ask()``. The policy is consulted first:

    - high-stakes / no history / revoked  → still ask the human
    - act / act_and_notify                 → auto-approve (record
      nothing new; the caller should still notify if act_and_notify)
    - after an explicit human verdict, the policy is updated so
      trust can grow or reset
    """

    def __init__(
        self,
        decide,
        timeout_seconds: int = 900,
        policy: "AutonomyPolicy | None" = None,
    ):
        self._decide = decide
        self.timeout_seconds = timeout_seconds
        self.policy = policy

    def ask(
        self,
        kind: str,
        title: str,
        body: str,
        *,
        action_class: str | None = None,
        stakes: str | None = None,
        force_ask: bool = False,
        reversible: bool = False,
        context: dict | None = None,
    ) -> Decision:
        """Ask for a decision. kind == 'question' is not a decision.

        Questions are conversation turns: they are answered in
        chat, not through this gate, and they have no clock.
        Callers must route questions to chat before calling ask.

        Parameters
        ----------
        action_class:
            Optional class name for graduated autonomy
            (e.g. "skill", "publish", "skill_privileged").
            When the attached policy grants ACT or ACT_NOTIFY,
            the human is not prompted.
        stakes:
            Optional override of the policy stakes tier for this call.
        force_ask:
            If True, ignore the policy and always prompt (e.g. for
            plan approval or other one-shot high-consequence cards).
        reversible:
            Caller's statement that this action cannot send,
            publish, spend, delete, or install anything. The plain
            gate ignores it; a PreapprovalGate wrapper uses it to
            decide whether a "live" grant may skip the card.
        context:
            Optional run context for the deciding surface
            (run_id, conversation_id, source, whatsapp_to, ...).
            Forwarded to the decide callable only when it
            accepts a context parameter, so plain decide
            callables keep working unchanged.
        """
        if kind == "question":
            raise ValueError("questions are chat turns, not approval cards")

        # Graduated autonomy short-circuit (never for force_ask).
        # Only standing permission — a class at or over its threshold,
        # or a low-stakes class — skips the card. Partial trust
        # (ACT_NOTIFY below the threshold) is reported by the policy
        # as a hint, but the human is still asked for it.
        if (
            not force_ask
            and self.policy is not None
            and action_class
        ):
            verdict = self.policy.permission(
                action_class, stakes=stakes
            )
            action = verdict.get("action", "ask")
            why = verdict.get("why", "")
            if (
                action in ("act", "act_and_notify")
                and self.policy.standing(action_class, stakes=stakes)
            ):
                return Decision(
                    "auto_approved",
                    reason=f"{action}: {why}",
                )

        result: list[Decision] = []
        decide_kwargs: dict = {}
        if context is not None:
            import inspect as _inspect
            try:
                if "context" in _inspect.signature(self._decide).parameters:
                    decide_kwargs["context"] = context
            except (TypeError, ValueError):
                pass
        thread = threading.Thread(
            target=lambda: result.append(
                self._decide(kind, title, body, **decide_kwargs)),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=self.timeout_seconds)
        if not result:
            decision = Decision("rejected", reason="timeout")
        else:
            verdict = result[0].verdict
            if verdict not in ("approved", "rejected", "edited"):
                decision = Decision("rejected", reason="invalid verdict")
            else:
                decision = result[0]

        # Learn from explicit human decisions only.
        if (
            self.policy is not None
            and action_class
            and decision.verdict in ("approved", "rejected", "edited")
        ):
            if decision.verdict == "approved":
                self.policy.record_approval(
                    action_class, stakes=stakes
                )
            else:
                # edited and rejected both reset the streak — the
                # human intervened, so standing permission is gone.
                self.policy.record_refusal(
                    action_class, stakes=stakes
                )

        return decision
