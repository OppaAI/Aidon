"""Tests for /think: max thinking across surfaces.

Covers the sampling hook (reasoning_effort xhigh), the shared
command parser + per-conversation flag store, the HTTP payload
mapping, and the REPL toggle.
"""

import os

from src.agentic.think_effort import sampling_for
from src.interface.repl import Repl
from src.interface.surfaces import (
    CLI_HELP_TEXT, HELP_TEXT, parse_think_command, set_think,
    think_enabled, toggle_think)
from src.system.model_client import HttpModelClient


def test_think_max_sets_model_maximum_effort():
    sample = sampling_for("plan", think_max=True)
    assert sample["reasoning_effort"] == "xhigh"
    assert sample["think"] is True


def test_think_max_wins_over_env_pin(monkeypatch):
    monkeypatch.setenv("AIDON_EFFORT_PLAN", "low")
    sample = sampling_for("plan", think_max=True)
    assert sample["reasoning_effort"] == "xhigh"
    assert sample["think"] is True


def test_no_think_max_sends_no_reasoning_effort():
    sample = sampling_for("plan")
    assert "reasoning_effort" not in sample


def test_parse_think_command():
    assert parse_think_command("/think") == {"action": "toggle"}
    assert parse_think_command("/think  build a shed ") == {
        "action": "once", "goal": "build a shed"}
    assert parse_think_command("/thinking") is None
    assert parse_think_command("think about this") is None
    assert parse_think_command("/help") is None


def test_think_flag_store_round_trip(tmp_path):
    key = "conv-1"
    assert think_enabled(tmp_path, key) is False
    assert toggle_think(tmp_path, key) is True
    assert think_enabled(tmp_path, key) is True
    assert set_think(tmp_path, key, False) is False
    assert think_enabled(tmp_path, key) is False


def test_http_payload_carries_reasoning_effort():
    client = HttpModelClient(api_base="http://127.0.0.1:9",
                             api_key="k", model="muse-spark")
    payload = client._payload([], sampling={"reasoning_effort": "xhigh"})
    assert payload["reasoning_effort"] == "xhigh"
    plain = client._payload([], sampling={"temperature": 0.2})
    assert "reasoning_effort" not in plain


def test_help_texts_list_think_and_all_slash_commands():
    assert "/think" in HELP_TEXT
    assert "/web" in HELP_TEXT
    assert "/mirror" in HELP_TEXT
    assert "/remember" in HELP_TEXT
    assert "/think" in CLI_HELP_TEXT
    assert "/attach" in CLI_HELP_TEXT
    assert "/quit" in CLI_HELP_TEXT


def test_repl_think_toggle_uses_callback_not_handler():
    seen = []
    state = {"on": False}

    def on_think():
        state["on"] = not state["on"]
        return "on" if state["on"] else "off"

    feed = ["/think", "/think", "/think build a shed", "/quit"]
    out = []

    def input_fn(prompt=""):
        if not feed:
            raise EOFError
        return feed.pop(0)

    repl = Repl(lambda line: seen.append(line) or "reply",
                input_fn=input_fn, output_fn=out.append, on_think=on_think)
    repl.run()
    assert state["on"] is False  # toggled twice
    assert seen == ["/think build a shed"]  # one-shot reaches the handler
    assert out.count("on") >= 1 and out.count("off") >= 1
