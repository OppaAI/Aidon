"""Desktop authorization is scoped to one bounded task invocation."""
import json
from types import SimpleNamespace

from src.system.goal_tool_computer import register_computer_tools


def build(tmp_path, monkeypatch, grants=None, allowed=True):
    calls, executed = [], []

    class Driver:
        def __call__(self, action):
            executed.append(action)

    def approve(*args, **kwargs):
        calls.append((args, kwargs))
        return allowed, "approved" if allowed else "rejected"

    monkeypatch.setattr("src.capabilities.x11_driver.X11Driver", Driver)
    tools = {}
    register_computer_tools(
        SimpleNamespace(computer_driver="x11", kernel_mode=False), tools,
        approve, lambda name: tmp_path / name, model_chat=lambda _: None,
        approved_tasks=grants)
    return tools, calls, executed


def test_one_approval_covers_batch_but_not_next_call(tmp_path, monkeypatch):
    tools, calls, executed = build(tmp_path, monkeypatch)
    actions = [{"kind": "key", "key": "Tab"}, {"kind": "type", "text": "Hello"}]
    assert json.loads(tools["computer_use"](actions))["ok"]
    assert len(calls) == 1 and len(executed) == 2
    assert json.loads(tools["computer_use"](actions))["ok"]
    assert len(calls) == 2


def test_rejection_executes_nothing(tmp_path, monkeypatch):
    tools, calls, executed = build(tmp_path, monkeypatch, allowed=False)
    assert not json.loads(tools["computer_use"]([{"kind": "key", "key": "Tab"}]))["ok"]
    assert len(calls) == 1 and not executed


def test_proceed_grant_is_exact_and_consumed_once(tmp_path, monkeypatch):
    actions = [{"kind": "type", "text": "Hello"}]
    tools, calls, executed = build(tmp_path, monkeypatch, [
        {"tool": "computer_use", "args": {"actions": actions}}])
    assert json.loads(tools["computer_use"](actions))["ok"]
    assert not calls and len(executed) == 1
    assert json.loads(tools["computer_use"](actions))["ok"]
    assert len(calls) == 1


def test_guided_grant_cannot_authorize_different_task_or_larger_limit(tmp_path, monkeypatch):
    observed = []

    def run(description, chat, **kwargs):
        assert kwargs["approve"]("key", "Tab")
        assert kwargs["approve"]("type", "Hello")
        observed.append(description)
        return {"ok": True}

    monkeypatch.setattr("src.capabilities.computer_use.run_guided_task", run)
    tools, calls, _ = build(tmp_path, monkeypatch, [
        {"tool": "computer_use_task", "args": {"description": "Open gedit", "max_actions": 10}}])
    task = tools["computer_use_task"]
    assert json.loads(task("Open gedit", max_actions=20))["ok"]
    assert len(calls) == 1
    assert json.loads(task("Open browser"))["ok"]
    assert len(calls) == 2
    assert json.loads(task("Open gedit"))["ok"]
    assert len(calls) == 2  # matching Proceed grant
    assert json.loads(task("Open gedit"))["ok"]
    assert len(calls) == 3  # grant already consumed
    assert len(observed) == 4


def test_guided_task_accepts_legacy_task_argument_and_matches_plan_grant(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "src.capabilities.computer_use.run_guided_task",
        lambda description, *_args, **_kwargs: calls.append(description) or {"ok": True},
    )
    tools, approvals, _ = build(tmp_path, monkeypatch, grants=[
        {"tool": "computer_use_task", "args": {"task": "Open gedit"}},
    ])

    result = json.loads(tools["computer_use_task"](task="Open gedit"))

    assert result["ok"]
    assert calls == ["Open gedit"]
    assert approvals == []
