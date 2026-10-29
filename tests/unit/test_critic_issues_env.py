"""Critic issue cap: AIDON_CRITIC_MAX_ISSUES, default 5, clamped 1-20."""

import json

from src.agentic.critic import max_issues, review_plan
from src.agentic.loop import Plan


def _chat_with(n):
    def _chat(messages):
        return json.dumps({"issues": [f"gap {i}" for i in range(n)]})
    return _chat


def _plan():
    return Plan(steps=[], deliverables=["a.md"], criteria=[])


def test_max_issues_default_and_clamps(monkeypatch):
    monkeypatch.delenv("AIDON_CRITIC_MAX_ISSUES", raising=False)
    assert max_issues() == 5
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "2")
    assert max_issues() == 2
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "garbage")
    assert max_issues() == 5
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "0")
    assert max_issues() == 1
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "99")
    assert max_issues() == 20
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", " 3 ")
    assert max_issues() == 3


def test_review_plan_caps_issues(monkeypatch):
    monkeypatch.delenv("AIDON_CRITIC_MAX_ISSUES", raising=False)
    assert len(review_plan(_plan(), "goal", ["write"],
                           chat=_chat_with(8))) == 5
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "2")
    assert review_plan(_plan(), "goal", ["write"],
                       chat=_chat_with(8)) == ["gap 0", "gap 1"]
    monkeypatch.setenv("AIDON_CRITIC_MAX_ISSUES", "garbage")
    assert len(review_plan(_plan(), "goal", ["write"],
                           chat=_chat_with(8))) == 5


def test_review_plan_no_chat_is_empty():
    assert review_plan(_plan(), "goal", ["write"], chat=None) == []
