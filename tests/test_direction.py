"""Tests for the human-direction package (src/agentic/direction.py)."""

import pytest

from src.agentic.direction import (
    PAID_PROVIDERS,
    AttentionNotifier,
    ChoicePolicy,
    blocked_note,
    is_paid_provider,
)


# ------------------------------------------------------------- ChoicePolicy

@pytest.mark.parametrize("stakes,confidence,expected", [
    ("high", 1.0, True),
    ("high", 0.0, True),
    ("high", 0.99, True),
    ("medium", 0.0, True),
    ("medium", 0.69, True),
    ("medium", 0.7, False),
    ("medium", 0.9, False),
    ("medium", 1.0, False),
    ("low", 0.0, False),
    ("low", 1.0, False),
    ("unknown", 1.0, True),   # unknown stakes treated as high
    ("HIGH", 1.0, True),      # case-insensitive
    ("", 1.0, True),          # empty stakes treated as high
    (None, 1.0, True),        # missing stakes treated as high
])
def test_should_ask_matrix(stakes, confidence, expected):
    assert ChoicePolicy().should_ask(stakes, confidence) is expected


def test_decide_asks_without_picking():
    options = ["a", "b"]
    result = ChoicePolicy().decide("high", 1.0, options)
    assert result == {"action": "ask", "options": ["a", "b"]}
    # The policy routes; it never picks an option itself.
    assert "choice" not in result and "selected" not in result


def test_decide_decides_without_picking():
    options = ["a", "b"]
    result = ChoicePolicy().decide("low", 0.0, options)
    assert result == {"action": "decide", "options": ["a", "b"]}


def test_decide_medium_boundary():
    policy = ChoicePolicy()
    assert policy.decide("medium", 0.69, [])["action"] == "ask"
    assert policy.decide("medium", 0.7, [])["action"] == "decide"


# --------------------------------------------------------- AttentionNotifier

def _recorder(calls, name, ok=True, fail=False):
    def send(message):
        calls.append((name, message))
        if fail:
            raise RuntimeError("channel down")
        return ok
    return send


def test_notifier_delivers_to_all_channels():
    calls = []
    notifier = AttentionNotifier({
        "slack": _recorder(calls, "slack"),
        "webui": _recorder(calls, "webui"),
        "cli": _recorder(calls, "cli"),
    })
    result = notifier.notify("hello")
    assert result == {"delivered": ["slack", "webui", "cli"],
                      "failed": [], "skipped": []}
    assert [name for name, _ in calls] == ["slack", "webui", "cli"]
    assert all(message == "hello" for _, message in calls)


def test_notifier_failing_channel_does_not_block_others():
    calls = []
    notifier = AttentionNotifier({
        "slack": _recorder(calls, "slack", fail=True),
        "webui": _recorder(calls, "webui"),
    })
    result = notifier.notify("hello")
    assert result["delivered"] == ["webui"]
    assert result["failed"] == ["slack"]
    assert result["skipped"] == []
    assert ("webui", "hello") in calls


def test_notifier_false_return_counts_as_failed():
    notifier = AttentionNotifier({
        "slack": _recorder([], "slack", ok=False),
        "webui": _recorder([], "webui"),
    })
    result = notifier.notify("hello")
    assert result["delivered"] == ["webui"]
    assert result["failed"] == ["slack"]


def test_notifier_unconfigured_channel_is_skipped():
    notifier = AttentionNotifier({"slack": None, "webui": _recorder([], "webui")})
    result = notifier.notify("hello")
    assert result["delivered"] == ["webui"]
    assert result["failed"] == []
    assert result["skipped"] == ["slack"]


def test_notifier_only_restricts_channels():
    calls = []
    notifier = AttentionNotifier({
        "slack": _recorder(calls, "slack"),
        "webui": _recorder(calls, "webui"),
        "cli": _recorder(calls, "cli"),
    })
    result = notifier.notify("hello", only=["cli"])
    assert result == {"delivered": ["cli"], "failed": [], "skipped": []}
    assert [name for name, _ in calls] == ["cli"]


def test_notifier_only_unknown_name_is_skipped():
    notifier = AttentionNotifier({"slack": _recorder([], "slack")})
    result = notifier.notify("hello", only=["telegram"])
    assert result == {"delivered": [], "failed": [], "skipped": ["telegram"]}


# ------------------------------------------------------------ blocked_note

def test_blocked_note_shape():
    assert blocked_note("no API key") == {"blocked": True, "reason": "no API key"}
    assert blocked_note(42) == {"blocked": True, "reason": "42"}


# ------------------------------------------------------------ no-paid rule

def test_paid_providers_contains_bright_data():
    assert "bright_data" in PAID_PROVIDERS


def test_is_paid_provider_case_insensitive():
    assert is_paid_provider("bright_data") is True
    assert is_paid_provider("Bright_Data") is True
    assert is_paid_provider("BRIGHT_DATA") is True
    assert is_paid_provider(" bright_data ") is True


def test_is_paid_provider_unknown_is_false():
    assert is_paid_provider("duckduckgo") is False
    assert is_paid_provider("") is False
    assert is_paid_provider(None) is False
    assert is_paid_provider(42) is False
