"""Sixth-audit fixes, part C: swarm double-run, Budget thread
safety, adapted-DAG replan routing, sandbox network-on contract.
"""
import json
import threading

import pytest

import src.agentic.dag as dag_mod
import src.agentic.adapt as adapt_mod
from src.agentic.budgets import Budget
from src.agentic.loop import GoalRunner
from src.agentic.swarm import run_swarm
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


# ---- 1. swarm double-run -------------------------------------------------

def test_swarm_no_subgoals_never_calls_runner():
    calls = []
    result = run_swarm("Do one thing", FakeClient("SINGLE"),
                       lambda goal: calls.append(goal) or "single done")
    # The caller runs the goal once itself on this shape, so
    # run_swarm returns empty results and never calls runner.
    assert result == {"swarmed": False, "subgoals": [], "results": [],
                      "merged": None}
    assert calls == []


def test_swarm_empty_reply_never_calls_runner():
    calls = []
    result = run_swarm("Do one thing", FakeClient(""),
                       lambda goal: calls.append(goal) or "single done")
    assert result["swarmed"] is False
    assert result["results"] == [] and result["merged"] is None
    assert calls == []


def test_swarm_split_still_runs_subgoals():
    result = run_swarm("Do two things",
                       FakeClient("1. Research A\n2. Research B"),
                       lambda subgoal: f"Done {subgoal}")
    assert result["swarmed"] is True
    assert result["subgoals"] == ["Research A", "Research B"]
    assert result["results"] == ["Done Research A", "Done Research B"]
    assert "## Research A" in result["merged"]


# ---- 2. Budget thread safety ----------------------------------------------

def test_budget_defaults_unchanged():
    budget = Budget()
    assert (budget.max_tokens, budget.max_steps, budget.max_seconds) == (200000, 50, 3600)
    assert budget.to_dict()["tokens_used"] == 0


def test_budget_counts_survive_parallel_subruns():
    budget = Budget(max_tokens=10 ** 9, max_steps=10 ** 9, max_seconds=10 ** 9)
    budget.start()

    def work():
        for _ in range(250):
            budget.add_step()
            budget.add_tokens("x", exact=4)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.steps_used == 2000
    assert budget.tokens_used == 8000
    assert budget.to_dict()["steps_used"] == 2000


# ---- helpers for adapted-plan tests ----------------------------------------

class _FakePoint:
    def __init__(self, payload):
        self.payload = payload
        self.text = payload.get("goal", "")
        self.id = "exp-1"


class _FakeHit:
    def __init__(self, point):
        self.point = point
        self.score = 1.0


class _FakeMemory:
    """recall() returns one experience; the rest of the
    memory surface is stubbed so a successful run completes."""

    def __init__(self, payload):
        self._payload = payload

    def recall(self, collection, query, limit=1, graph=False):
        return [_FakeHit(_FakePoint(self._payload))]

    def record_episodic(self, project_id, goal, data):
        return object()

    def auto_promote_enabled(self):
        return False


def _experience_payload(goal, nodes):
    return {"goal": goal, "goal_shape": goal, "dag_nodes": json.dumps(nodes),
            "tools_used": [], "skills_used": [], "outcome": "done",
            "lesson": "", "generalized_nodes": "[]"}


def _runner(tmp_path, memory=None, researcher=None, client=None, network_available=None):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved", "test"))
    return GoalRunner(client or FakeClient(""),
                      Sandbox(tmp_path / "work"), gate, tmp_path,
                      memory=memory, researcher=researcher,
                      network_available=network_available)


def _plan_json(filename):
    return json.dumps({"steps": [{"tool": "write",
                                  "args": {"file": filename, "text": "hi"},
                                  "deliverable": filename}],
                       "deliverables": [filename], "criteria": []})


# ---- 3. adapted-DAG replan routing ------------------------------------------

def test_adapted_dag_plan_goes_through_replan(tmp_path):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or [])
    seen = []
    original = runner._research_and_replan

    def spy(plan, goal_arg, spec=None):
        seen.append(plan)
        return original(plan, goal_arg, spec=spec)

    runner._research_and_replan = spy
    report = runner.run(goal)
    # The adapted plan reached the research+replan pass, even
    # though it carries no open questions (so the researcher
    # is correctly not called and the plan is unchanged).
    assert len(seen) == 1
    assert seen[0].deliverables == ["a.md"]
    assert researcher_calls == []
    assert report.status == "done"
    assert "a.md" in report.files


def test_adapted_dag_plan_with_needs_research_gets_research(tmp_path, monkeypatch):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    real_adapt = dag_mod.adapt_dag

    def adapt_with_questions(experience, new_goal, client=None):
        dag, summary, model_calls = real_adapt(experience, new_goal, client=client)
        return dag, {**summary, "needs_research": ["price of X"]}, model_calls

    monkeypatch.setattr(dag_mod, "adapt_dag", adapt_with_questions)
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or ["note"],
                     client=FakeClient(_plan_json("b.md")))
    report = runner.run(goal)
    assert researcher_calls == ["price of X"]
    # The replanned plan (b.md), not the adapted plan (a.md), ran.
    assert report.status == "done"
    assert "b.md" in report.files
    assert (tmp_path / "work" / "b.md").exists()
    assert report.adapted is True


def test_adapted_experience_nodes_with_needs_research_get_research(tmp_path, monkeypatch):
    goal = "Write a report"
    memory = _FakeMemory(_experience_payload(goal, [
        {"tool": "write", "args": {"file": "a.md", "text": "hi"},
         "deliverable": "a.md"}]))
    monkeypatch.setattr(dag_mod, "adapt_dag", lambda *a, **k: None)
    monkeypatch.setattr(adapt_mod, "adapt_experience",
                        lambda experience, new_goal, client=None: (
                            [{"tool": "write", "args": {"file": "c.md", "text": "hi"},
                              "deliverable": "c.md",
                              "needs_research": ["auth shape of Y"]}], 1, 0))
    researcher_calls = []
    runner = _runner(tmp_path, memory=memory,
                     researcher=lambda item: researcher_calls.append(item) or ["note"],
                     client=FakeClient(_plan_json("d.md")))
    report = runner.run(goal)
    assert researcher_calls == ["auth shape of Y"]
    assert report.status == "done"
    assert "d.md" in report.files
    assert (tmp_path / "work" / "d.md").exists()


# ---- 4. sandbox network-on contract ------------------------------------------

def _run_simple_goal(tmp_path, network_available):
    bodies = []
    gate = ApprovalGate(lambda kind, title, body: bodies.append(body) or Decision("approved", "test"))
    runner = GoalRunner(FakeClient(_plan_json("e.md")),
                        Sandbox(tmp_path / "work"), gate, tmp_path,
                        network_available=network_available)
    return runner, bodies


def test_network_on_announces_reports_and_marks_card(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, True)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is True
    assert "Sandbox network: on" in capsys.readouterr().err
    assert any("Sandbox network: on" in body for body in bodies)


def test_network_none_says_nothing(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, None)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is None
    assert "Sandbox network: on" not in capsys.readouterr().err
    assert all("Sandbox network: on" not in body for body in bodies)


def test_network_false_says_nothing(tmp_path, capsys):
    runner, bodies = _run_simple_goal(tmp_path, False)
    report = runner.run("Write e.md")
    assert report.status == "done"
    assert report.network is False
    assert "Sandbox network: on" not in capsys.readouterr().err
    assert all("Sandbox network: on" not in body for body in bodies)


def test_network_default_is_unknown_and_registry_kwarg_optional(tmp_path):
    # ToolRegistry does not accept network_available yet (a
    # sibling change owns it); constructing a runner with a
    # network value must still work, with the value stored.
    runner, _ = _run_simple_goal(tmp_path, True)
    assert runner.network_available is True
    assert runner.tools is not None
