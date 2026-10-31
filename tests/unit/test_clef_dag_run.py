"""Clef DAG ranking inside run(): the winner feeds adaptation,
and a veto falls through to a fresh plan."""

import json
from types import SimpleNamespace

from src.agentic.loop import GoalRunner, Plan
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.system.model_client import FakeClient


class _StubClef:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence

    def decide_choice(self, state, name, instructions, criteria):
        return self.choice, self.confidence, {}


def _runner(work, tmp_path, clef):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    memory = SimpleNamespace(
        record_episodic=lambda *args: SimpleNamespace())
    return GoalRunner(FakeClient("hi"), Sandbox(work), gate, tmp_path,
                      memory=memory, clef_client=clef)


def _noop_store(*args):
    return None


def _composed_plan(capture):
    def _compose(experiences, goal):
        capture.append([e.goal for e in experiences])
        if not experiences:
            # Mirrors the real _composed_plan: composition needs
            # at least two recalled workflows; empty recall is None.
            return None
        return (Plan(
            steps=[{"tool": "write",
                    "args": {"file": "out.txt", "text": "adapted"},
                    "deliverable": "out.txt"}],
            deliverables=["out.txt"],
            criteria=["out.txt exists"]), "composed")
    return _compose


def test_ranked_winner_feeds_composition(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path, _StubClef("dag_2", 0.9))
    exp_a = SimpleNamespace(goal="snake", dag_nodes=[])
    exp_b = SimpleNamespace(goal="worm", dag_nodes=[])
    monkeypatch.setattr("src.memory.experience.recall_experiences",
                        lambda memory, goal, limit=3: [exp_a, exp_b])
    monkeypatch.setattr("src.memory.experience.matching_failure",
                        lambda memory, goal: None)
    monkeypatch.setattr("src.memory.experience.store_experience",
                        _noop_store)
    capture = []
    runner._composed_plan = _composed_plan(capture)
    report = runner.run("make a worm game")
    assert report.status == "done"
    assert capture and capture[0][0] == "worm", \
        "composition must see the Clef winner first"


def test_veto_falls_through_to_fresh_plan(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    runner = _runner(work, tmp_path, _StubClef("none", 0.95))
    exp_a = SimpleNamespace(goal="snake", dag_nodes=[])
    exp_b = SimpleNamespace(goal="worm", dag_nodes=[])
    monkeypatch.setattr("src.memory.experience.recall_experiences",
                        lambda memory, goal, limit=3: [exp_a, exp_b])
    monkeypatch.setattr("src.memory.experience.matching_failure",
                        lambda memory, goal: None)
    monkeypatch.setattr("src.memory.experience.store_experience",
                        _noop_store)
    capture = []
    runner._composed_plan = _composed_plan(capture)
    report = runner.run("make something unrelated")
    assert report.status == "done"
    assert capture == [[]], \
        "composition must see the vetoed, emptied recall (and return None)"
    assert "out.txt" not in (report.files or []), \
        "the composed plan must not have run after a veto"
