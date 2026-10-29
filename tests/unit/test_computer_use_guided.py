"""Guided computer-use task (observe-act loop) and secret handles.

The pre-written action list is blind: the model writes every
click before seeing the screen. The guided loop closes that
gap — screenshot, vision description, one decided action at a
time — and secret handles keep credential values out of
model-visible text entirely.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.capabilities.computer_use import run_computer_use, run_guided_task
from src.system.main import _goal_extra_tools


def _fake_png(path):
    Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return True


class _ScriptChat:
    """model_chat stub: pops replies in call order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


class _Driver:
    def __init__(self, fail_with=None):
        self.actions = []
        self.fail_with = fail_with

    def __call__(self, action):
        self.actions.append(action)
        if self.fail_with is not None:
            raise RuntimeError(self.fail_with)


def test_secret_handle_resolves_for_driver_only(tmp_path):
    driver = _Driver()
    result = run_computer_use(
        [{"kind": "type", "text": "pass {{secret.PW}}"}],
        approve=lambda kind, detail: True, driver=driver,
        screenshot_dir=str(tmp_path), secrets={"PW": "hunter2"})
    assert result["ok"] is True
    assert driver.actions[0]["text"] == "pass hunter2"
    assert result["executed"][0]["action"]["text"] == "pass {{secret.PW}}"


def test_unissued_secret_handle_refused(tmp_path):
    driver = _Driver()
    for secrets in ({}, None):
        result = run_computer_use(
            [{"kind": "type", "text": "pass {{secret.PW}}"}],
            approve=lambda kind, detail: True, driver=driver,
            screenshot_dir=str(tmp_path), secrets=secrets)
        assert result["ok"] is False
        assert "not issued" in result["error"]
    assert driver.actions == []


def test_driver_error_redacts_secret_values(tmp_path):
    driver = _Driver(fail_with="boom hunter2")
    result = run_computer_use(
        [{"kind": "type", "text": "pass {{secret.PW}}"}],
        approve=lambda kind, detail: True, driver=driver,
        screenshot_dir=str(tmp_path), secrets={"PW": "hunter2"})
    assert result["ok"] is False
    assert "hunter2" not in result["error"]
    assert "[secret]" in result["error"]


def test_guided_task_sees_screen_then_acts_then_finishes(tmp_path):
    driver = _Driver()
    chat = _ScriptChat([
        "A login form with a username field.",
        json.dumps({"action": {"kind": "type", "text": "hello"},
                    "done": False, "reason": "fill the field"}),
        "The form is submitted.",
        json.dumps({"action": None, "done": True,
                    "reason": "goal achieved"}),
    ])
    result = run_guided_task(
        "submit the form", chat,
        approve=lambda kind, detail: True, driver=driver,
        screenshot_dir=str(tmp_path), capture=_fake_png)
    assert result["ok"] is True
    assert len(result["actions_taken"]) == 1
    assert driver.actions[0] == {"kind": "type", "text": "hello"}
    assert (tmp_path / "screenshot_round_0.png").exists()
    assert (tmp_path / "screenshot_round_1.png").exists()
    trace = json.loads((tmp_path / "task_trace.json").read_text())
    assert trace["goal"] == "submit the form"
    assert len(trace["steps"]) == 1


def test_guided_task_stops_at_max_actions(tmp_path):
    driver = _Driver()
    chat = _ScriptChat([
        "Screen.", json.dumps({"action": {"kind": "key", "key": "Tab"},
                               "done": False, "reason": "move"}),
        "Screen.", json.dumps({"action": {"kind": "key", "key": "Tab"},
                               "done": False, "reason": "move"}),
    ])
    result = run_guided_task(
        "keep tabbing", chat,
        approve=lambda kind, detail: True, driver=driver,
        screenshot_dir=str(tmp_path), capture=_fake_png, max_actions=2)
    assert result["ok"] is False
    assert "max actions" in result["error"]
    assert len(result["actions_taken"]) == 2


def test_guided_task_refuses_without_model_chat(tmp_path):
    driver = _Driver()
    result = run_guided_task("do it", None, driver=driver,
                             screenshot_dir=str(tmp_path),
                             capture=_fake_png)
    assert result["ok"] is False
    assert driver.actions == []


def test_handler_wires_guided_task_only_with_model_chat(tmp_path, monkeypatch):
    import src.capabilities.x11_driver as _x11
    monkeypatch.setattr(_x11, "X11Driver", lambda: _Driver())
    config = SimpleNamespace(home=tmp_path, computer_driver="x11",
                             kernel_mode=False,
                             owner_email="owner@example.com")
    tools = _goal_extra_tools(config, tmp_path,
                              model_chat=lambda messages: "{}")
    assert "computer_use" in tools
    assert "computer_use_task" in tools
    tools_no_chat = _goal_extra_tools(config, tmp_path)
    assert "computer_use_task" not in tools_no_chat
