"""Proofs for the outside-review gaps: a failed example must
change a later decision, verification must catch substantive
errors (not just missing files), and an invented tool must be
rejected instead of executed.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner, Plan, Step
from src.agentic.verify import verify_plan
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.memory.experience import record_failure
from src.system.model_client import FakeClient


class _StubMemory:
    """Minimal memory: record stores payloads, recall returns them."""

    def __init__(self):
        self.points = []

    def record(self, collection, text, payload):
        point = SimpleNamespace(payload=payload, text=text)
        self.points.append(point)
        return point

    def recall(self, collection, query, limit=3, graph=False):
        return [SimpleNamespace(point=p, score=1.0)
                for p in self.points[:limit]]


def _runner(tmp_path, client, memory):
    gate = ApprovalGate(
        lambda kind, title, body: Decision("approved"))
    return GoalRunner(client, Sandbox(tmp_path / "work", 120), gate,
                      tmp_path, memory=memory,
                      use_semantic_router_skip=True)


def _plan_text(client):
    return "\n".join(
        str(m.get("content", "")) if isinstance(m, dict) else str(m)
        for call in client.calls for m in call)


def test_failed_example_steers_similar_goal_not_different_one(tmp_path):
    memory = _StubMemory()
    record_failure(memory, "Write a snake game", "crashed on start",
                   ["shell"], ["snake.py"])
    similar = FakeClient("{}")
    _runner(tmp_path / "a", similar, memory).run(
        "Write a snake game with levels")
    assert "Do not repeat" in _plan_text(similar)
    different = FakeClient("{}")
    _runner(tmp_path / "b", different, memory).run("Fix the car")
    assert "Do not repeat" not in _plan_text(different)


def test_finalize_failed_records_failure_partial_does_not(tmp_path, monkeypatch):
    from src.agentic.loop import RunReport
    import src.memory.experience as _exp
    recorded = []
    monkeypatch.setattr(_exp, "record_failure",
                        lambda *a: recorded.append(a))
    runner = _runner(tmp_path, FakeClient("{}"), _StubMemory())
    plan = Plan(steps=[], deliverables=["x.md"], criteria=[])
    runner._finalize(RunReport("failed", "g", plan, False, False,
                               error="boom"))
    assert len(recorded) == 1
    assert recorded[0][1] == "g"
    runner._finalize(RunReport("partial", "g", plan, False, False,
                               files=["x.md"]))
    assert len(recorded) == 1


def test_verification_catches_wrong_content_and_wrong_totals(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "report.md").write_text("total: 50\n")
    ok, failures, _ = verify_plan(
        workdir, ["report.md"], ["report.md contains total: 90"])
    assert not ok and any("lacks" in f for f in failures)
    ok, _, _ = verify_plan(
        workdir, ["report.md"], ["report.md contains total: 50"])
    assert ok
    (workdir / "ledger.md").write_text(
        '{"lines": [{"amount_cents": 100}, {"amount_cents": 250}]}')
    ok, failures, _ = verify_plan(
        workdir, ["ledger.md"], ["ledger ledger.md total is 999"])
    assert not ok and any("!=" in f for f in failures)
    ok, _, _ = verify_plan(
        workdir, ["ledger.md"], ["ledger ledger.md total is 350"])
    assert ok


def test_currency_itemized_total_checks_budget_and_stated_total(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = workdir / "grocery-plan.md"
    plan.write_text(
        "## Shopping List\n"
        "- Produce box - $13.10\n"
        "- Pantry items - $21.40\n"
        "- Dairy and eggs - $9.00\n"
        "Estimated Total: $38.20\n"
    )

    criteria = [
        "currency items in grocery-plan.md total at most $40.00",
        "currency items in grocery-plan.md match stated total",
    ]
    ok, failures, _ = verify_plan(workdir, ["grocery-plan.md"], criteria)
    assert not ok
    assert any("exceeds $40.00" in failure for failure in failures)
    assert any("!= stated $38.20" in failure for failure in failures)

    plan.write_text(
        "## Shopping List\n"
        "- Produce box - $13.10\n"
        "- Pantry items - $21.40\n"
        "- Dairy and eggs - $5.00\n"
        "Estimated Total: $39.50\n"
    )
    ok, failures, _ = verify_plan(workdir, ["grocery-plan.md"], criteria)
    assert ok, failures


def test_budgeted_meal_plan_gets_currency_checks_even_if_plan_omits_them(tmp_path):
    from src.agentic.verify import budget_currency_criteria

    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = workdir / "grocery-plan.md"
    plan.write_text(
        "## Shopping List\n"
        "- Bananas - $1.50\n"
        "- Apples - $3.00\n"
        "- Spinach - $2.50\n"
        "- Lettuce - $1.80\n"
        "- Carrots - $1.50\n"
        "- Onion - $0.80\n"
        "- Frozen vegetables - $2.00\n"
        "- Oats - $3.50\n"
        "- Tortillas - $2.50\n"
        "- Rice - $1.50\n"
        "- Pasta - $1.20\n"
        "- Marinara - $2.00\n"
        "- Black beans - $1.80\n"
        "- White beans - $0.90\n"
        "- Salsa - $2.50\n"
        "- Peanut butter - $2.50\n"
        "- Hummus - $2.00\n"
        "- Soy sauce - $1.00\n"
        "- Eggs - $2.50\n"
        "- Milk - $2.00\n"
        "- Cheese - $2.00\n"
        "- Yogurt - $2.50\n"
        "Estimated Total: $38.20\n"
    )
    goal = "Make a 3-day vegetarian meal plan with a shopping list under $40."
    criteria = budget_currency_criteria(goal, ["grocery-plan.md"], workdir)

    assert criteria == [
        "currency items in grocery-plan.md total at most $40",
        "currency items in grocery-plan.md match stated total",
    ]
    ok, failures, _ = verify_plan(workdir, ["grocery-plan.md"], criteria)
    assert not ok
    assert any("total $43.50 exceeds $40.00" in failure for failure in failures)
    assert any("!= stated $38.20" in failure for failure in failures)


def test_invented_tool_plan_is_rejected_not_executed(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MAX_PLAN_REPAIRS", "0")
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    runner = _runner(tmp_path, FakeClient("{}"), None)
    runner._timeboxed = False
    runner._deadline_at = None
    plan = Plan(steps=[
        Step(tool="playwright_pdf",
             args={"url": "https://example.com"},
             deliverable="page.pdf")],
        deliverables=["page.pdf"], criteria=[])
    report = runner._execute_plan("make a PDF with playwright", plan)
    assert report.status == "failed"
    assert "not allowed" in (report.error or "")
    assert not (tmp_path / "work" / "page.pdf").exists()
