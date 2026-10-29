"""Risk-based critic gate.

Default mode is risk: the critic runs only when the plan
earns scrutiny — a fresh large plan, or any plan with a
live step (post/send/publish). Adapted plans already proved
their shape and small plans have little to sense-check, so
both skip. "always" restores a critic pass on every plan;
an unknown mode falls back to risk; 0 small-plan steps
means only adapted plans skip.
"""

import pytest

from src.agentic.loop import Plan, Step, _critic_wanted, _resolve_critic_mode


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("AIDON_CRITIC_MODE", raising=False)
    monkeypatch.delenv("AIDON_CRITIC_RISK_MAX_STEPS", raising=False)


def _plan(*tools):
    return Plan(
        steps=[Step(tool=tool, deliverable=f"f{index}.txt")
               for index, tool in enumerate(tools)],
        deliverables=[], criteria=[])


def test_default_mode_is_risk():
    assert _resolve_critic_mode() == "risk"


def test_fresh_small_plan_skips():
    assert _critic_wanted(_plan("write")) is False


def test_fresh_large_plan_runs():
    assert _critic_wanted(
        _plan("write", "read", "list", "shell", "calculate")) is True


def test_adapted_large_plan_skips():
    assert _critic_wanted(
        _plan("write", "read", "list", "shell", "calculate"),
        "old goal") is False


def test_small_live_plan_runs():
    assert _critic_wanted(_plan("email_send")) is True


def test_adapted_live_plan_runs():
    assert _critic_wanted(_plan("write", "email_send"), "old goal") is True


def test_always_mode_runs_everything(monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_MODE", "always")
    assert _critic_wanted(_plan("write")) is True
    assert _critic_wanted(_plan("write"), "old goal") is True


def test_unknown_mode_falls_back_to_risk(monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_MODE", "bogus")
    assert _resolve_critic_mode() == "risk"
    assert _critic_wanted(_plan("write")) is False


def test_zero_small_steps_means_only_adapted_skips(monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_RISK_MAX_STEPS", "0")
    assert _critic_wanted(_plan("write")) is True
    assert _critic_wanted(_plan("write"), "old goal") is False


def test_custom_small_threshold(monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_RISK_MAX_STEPS", "5")
    assert _critic_wanted(
        _plan("write", "read", "list", "shell", "calculate")) is False
    assert _critic_wanted(
        _plan("write", "read", "list", "shell", "calculate", "llm")) is True
