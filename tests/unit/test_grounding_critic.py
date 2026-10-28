"""Grounding rule + plan critic: skill-as-tool rejection, model
reflection before execution, bounded repair, never blocking."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.critic import review_plan
from src.agentic.plan_guard import validate_plan as guard_validate
from src.agentic.validator import validate_plan


class _Step:
    def __init__(self, tool, args=None, deliverable="out"):
        self.tool = tool
        self.args = args or {}
        self.deliverable = deliverable


class _Plan:
    def __init__(self, steps, deliverables=None):
        self.steps = steps
        self.deliverables = deliverables if deliverables is not None \
            else [s.deliverable for s in steps]

    def normalized_steps(self):
        return self.steps


def test_validator_names_skill_as_tool():
    plan = _Plan([_Step("dinner-pdf", deliverable="menu.pdf")])
    failures = validate_plan(plan, allowed_tools={"write"},
                             skill_names={"dinner-pdf"})
    assert len(failures) == 1
    assert "is a skill, not a callable tool" in failures[0]


def test_validator_unknown_tool_message_unchanged():
    plan = _Plan([_Step("teleport", deliverable="x")])
    failures = validate_plan(plan, allowed_tools={"write"},
                             skill_names={"dinner-pdf"})
    assert failures == ["step 0: tool teleport is not allowed"]


def test_guard_validator_names_skill_as_tool():
    plan = {"steps": [{"id": "s1", "tool": "dinner-pdf",
                       "args": {}, "deliverable": "menu.pdf"}]}
    failures = guard_validate(plan, {"write"}, skill_names={"dinner-pdf"})
    assert len(failures) == 1
    assert "as a tool" in failures[0]


def test_critic_without_client_returns_no_issues():
    plan = _Plan([_Step("write", deliverable="a.md")])
    assert review_plan(plan, "write a report", {"write"}) == []


def test_critic_parses_issue_list():
    plan = _Plan([_Step("write", deliverable="a.md")])

    def chat(messages):
        assert messages[0].role == "system"
        return '{"issues": ["no research step for a research goal"]}'
    issues = review_plan(plan, "research X and write a report",
                         {"write"}, chat=chat)
    assert issues == ["no research step for a research goal"]


def test_critic_bad_json_and_chat_failure_are_clean():
    plan = _Plan([_Step("write", deliverable="a.md")])
    assert review_plan(plan, "x", {"write"},
                       chat=lambda m: "not json at all") == []

    def boom(messages):
        raise RuntimeError("down")
    assert review_plan(plan, "x", {"write"}, chat=boom) == []


def test_critic_caps_issues_at_five():
    plan = _Plan([_Step("write", deliverable="a.md")])

    def chat(messages):
        import json
        return json.dumps({"issues": [f"issue {i}" for i in range(9)]})
    assert len(review_plan(plan, "x", {"write"}, chat=chat)) == 5
