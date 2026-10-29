"""Skill level 2: the planner asks for a full skill text by name.

Level 1 is the 200-char head in every plan prompt. Level 2 is
one expansion pass: a plan whose JSON lists skill names gets
re-planned with those skills' full texts injected. Unknown
names are ignored, never a failure; the expanded plan's own
list never triggers a further pass.
"""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner


class _ScriptedClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, sampling=None):
        self.calls.append(messages)
        return self.replies.pop(0)


def _plan_json(skills, deliverables=("a.txt",)):
    return json.dumps({
        "steps": [{"tool": "write", "args": {"file": "a.txt"},
                   "deliverable": "a.txt"}],
        "deliverables": list(deliverables),
        "criteria": [],
        "skills": list(skills),
    })


def _runner(skill_texts, client, kernel_mode=False):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = client
    runner.router = None
    runner.plugins = SimpleNamespace(
        tool_names=lambda: [], skill_texts=lambda: dict(skill_texts))
    runner.kernel_mode = kernel_mode
    runner.extra_tools = {}
    runner.memory = None
    runner.budget = None
    runner.guidance = ""
    runner.image_paths = []
    runner.stream_callback = None
    runner._effort_escalated = False
    runner._trace = lambda *args, **kwargs: None
    return runner


def _system_text(call):
    return str(call[0].content)


def test_level2_expands_once_and_ignores_second_request():
    full = ("Deploy skill short head. " * 20
            + "FULL_TEXT_MARKER_PAST_HEAD "
            + "Step one, step two, step three. " * 20)
    assert "FULL_TEXT_MARKER_PAST_HEAD" not in full[:200]
    client = _ScriptedClient([_plan_json(["deploy.md"]),
                              _plan_json(["deploy.md"])])
    runner = _runner({"deploy.md": full}, client)
    plan = runner._plan("Deploy the thing")
    assert len(client.calls) == 2
    assert "FULL_TEXT_MARKER_PAST_HEAD" not in _system_text(client.calls[0])
    assert "FULL_TEXT_MARKER_PAST_HEAD" in _system_text(client.calls[1])
    assert plan.deliverables == ["a.txt"]


def test_level2_unknown_skill_name_ignored():
    client = _ScriptedClient([_plan_json(["nope.md"])])
    runner = _runner({"deploy.md": "Deploy skill text."}, client)
    plan = runner._plan("Deploy the thing")
    assert len(client.calls) == 1
    assert plan.deliverables == ["a.txt"]


def test_level2_no_request_single_call():
    client = _ScriptedClient([_plan_json([])])
    runner = _runner({"deploy.md": "Deploy skill text."}, client)
    runner._plan("Deploy the thing")
    assert len(client.calls) == 1


def test_level2_bare_name_resolves_plugin_skill():
    full = "Deploy skill short head. " * 20 + "BARE_MARKER " + "body. " * 20
    client = _ScriptedClient([_plan_json(["deploy.md"]),
                              _plan_json([])])
    runner = _runner({"myplugin/deploy.md": full}, client)
    runner._plan("Deploy the thing")
    assert len(client.calls) == 2
    assert "BARE_MARKER" in _system_text(client.calls[1])


def test_level2_full_text_budget_cap():
    full = "x" * 21000 + "END_MARKER"
    client = _ScriptedClient([_plan_json(["big.md"]), _plan_json([])])
    runner = _runner({"big.md": full}, client)
    runner._plan("Do the big thing")
    assert len(client.calls) == 2
    second = _system_text(client.calls[1])
    assert "[cut here at the full-text budget]" in second
    assert "END_MARKER" not in second


def test_level2_skipped_in_kernel_mode():
    client = _ScriptedClient([_plan_json(["deploy.md"])])
    runner = _runner({"deploy.md": "Deploy skill text."}, client,
                     kernel_mode=True)
    runner._plan("Deploy the thing")
    assert len(client.calls) == 1
