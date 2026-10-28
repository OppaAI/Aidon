"""Dynamic sampling flag and per-phase effort pins."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from src.agentic.think_effort import effort_for, resolve_effort, sampling_for


def test_temp_override_applies_and_dynamic_flag_disables_it(monkeypatch):
    monkeypatch.setenv("AIDON_TEMP_RESPOND", "0.5")
    assert sampling_for("respond")["temperature"] == 0.5
    monkeypatch.setenv("AIDON_DYNAMIC_SAMPLING", "0")
    assert sampling_for("respond")["temperature"] == 0.8


def test_effort_pin_wins_over_table_and_is_never_escalated(monkeypatch):
    monkeypatch.setenv("AIDON_EFFORT_VERIFY", "low")
    assert effort_for("verify") == "low"
    assert resolve_effort("verify", escalate=True) == "low"


def test_effort_pin_invalid_value_is_ignored(monkeypatch):
    monkeypatch.setenv("AIDON_EFFORT_VERIFY", "extreme")
    assert effort_for("verify") == "low"
