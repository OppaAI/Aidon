"""Graduated autonomy wired into ApprovalGate."""

from src.interface.approval import ApprovalGate, Decision
from src.interface.autonomy import (
    ASK,
    STAKES_HIGH,
    STAKES_MEDIUM,
    policy_for,
)


def _always_approve(kind, title, body):
    return Decision("approved")


def _always_reject(kind, title, body):
    return Decision("rejected")


def test_high_stakes_never_auto_approves(tmp_path):
    policy = policy_for(tmp_path, threshold=2)
    # Even after many approvals, high stakes stays ASK.
    for _ in range(10):
        policy.record_approval("skill_privileged", stakes=STAKES_HIGH)
    gate = ApprovalGate(_always_approve, policy=policy)
    decision = gate.ask(
        "skill", "Trust privileged skill?", "body",
        action_class="skill_privileged", stakes=STAKES_HIGH,
    )
    # Human was still asked (our fake approved), not auto_approved.
    assert decision.verdict == "approved"
    assert decision.reason == ""


def test_medium_stakes_graduates_after_threshold(tmp_path):
    policy = policy_for(tmp_path, threshold=3, notify_threshold=2)
    gate = ApprovalGate(_always_approve, policy=policy)

    # First two explicit approvals build the streak.
    for i in range(2):
        d = gate.ask(
            "skill", f"approve {i}", "body",
            action_class="skill", stakes=STAKES_MEDIUM,
        )
        assert d.verdict == "approved"

    # Third pushes over threshold; next call should auto-approve
    # without calling the human decide function.
    calls = []

    def tracking_decide(kind, title, body):
        calls.append(kind)
        return Decision("approved")

    gate2 = ApprovalGate(tracking_decide, policy=policy)
    # One more explicit approval to cross threshold=3
    gate2.ask(
        "skill", "approve 2", "body",
        action_class="skill", stakes=STAKES_MEDIUM,
    )
    assert len(calls) == 1  # still asked this time

    # Now standing permission: human not prompted.
    d = gate2.ask(
        "skill", "should auto", "body",
        action_class="skill", stakes=STAKES_MEDIUM,
    )
    assert d.verdict == "auto_approved"
    assert "act" in d.reason
    assert len(calls) == 1  # no additional human call


def test_refusal_resets_streak(tmp_path):
    policy = policy_for(tmp_path, threshold=2)
    gate = ApprovalGate(_always_approve, policy=policy)
    gate.ask("skill", "a", "b", action_class="skill", stakes=STAKES_MEDIUM)
    gate.ask("skill", "a", "b", action_class="skill", stakes=STAKES_MEDIUM)

    # Standing permission should exist.
    d = ApprovalGate(lambda *a: Decision("approved"), policy=policy).ask(
        "skill", "x", "y", action_class="skill", stakes=STAKES_MEDIUM,
    )
    assert d.verdict == "auto_approved"

    # Explicit refusal resets.
    reject_gate = ApprovalGate(_always_reject, policy=policy)
    # force_ask so we actually get a human rejection recorded
    d = reject_gate.ask(
        "skill", "no", "body",
        action_class="skill", stakes=STAKES_MEDIUM, force_ask=True,
    )
    assert d.verdict == "rejected"

    # Streak gone — must ask again.
    calls = []

    def tracking(kind, title, body):
        calls.append(1)
        return Decision("approved")

    d = ApprovalGate(tracking, policy=policy).ask(
        "skill", "again", "body",
        action_class="skill", stakes=STAKES_MEDIUM,
    )
    assert d.verdict == "approved"
    assert len(calls) == 1


def test_force_ask_bypasses_policy(tmp_path):
    policy = policy_for(tmp_path, threshold=1)
    policy.record_approval("skill", stakes=STAKES_MEDIUM)
    calls = []

    def tracking(kind, title, body):
        calls.append(1)
        return Decision("approved")

    gate = ApprovalGate(tracking, policy=policy)
    d = gate.ask(
        "plan", "Approve plan", "body",
        action_class="skill", stakes=STAKES_MEDIUM, force_ask=True,
    )
    assert d.verdict == "approved"
    assert len(calls) == 1


def test_no_policy_behaves_as_before():
    """Gate without a policy is unchanged."""
    gate = ApprovalGate(_always_approve)
    d = gate.ask("live", "Go live?", "body")
    assert d.verdict == "approved"


def test_timeout_is_rejected_and_does_not_build_trust(tmp_path):
    policy = policy_for(tmp_path, threshold=1)

    def never_answer(kind, title, body):
        import time
        time.sleep(10)
        return Decision("approved")

    gate = ApprovalGate(never_answer, timeout_seconds=0.05, policy=policy)
    d = gate.ask(
        "skill", "slow", "body",
        action_class="skill", stakes=STAKES_MEDIUM,
    )
    assert d.verdict == "rejected"
    assert d.reason == "timeout"
    # Timeout is not consent — streak stays zero.
    assert policy.permission("skill", stakes=STAKES_MEDIUM)["action"] == ASK
Grok finished responding
