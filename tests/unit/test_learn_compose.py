"""Tests for failed-example learning and DAG composition."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.dag import DAG, DAGNode, compose_dags
from src.agentic.loop import GoalRunner, Plan, RunReport
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.memory.experience import (
    build_experience, matching_failure, recall_experiences,
    recall_failures, record_failure, store_experience)
from src.memory.memory import InMemoryStore, Memory
from src.system.model_client import FakeClient


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def _runner(tmp_path, memory):
    home = tmp_path / "home2"
    home.mkdir(exist_ok=True)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(FakeClient(""), sandbox, gate, home, memory=memory)


def _success(goal, deliverable):
    return build_experience(goal, "", [{"tool": "write",
                                       "args": {"file": deliverable,
                                                "text": f"Content for {deliverable}.\n"},
                                       "deliverable": deliverable}],
                            ["write"], [], "done", "")


def _dag(nodes):
    return DAG([DAGNode(id=n[0], tool="write", args={"file": n[1]},
                       deliverable=n[1], depends_on=n[2]) for n in nodes])


# --- failed examples ---

def test_failed_example_round_trip_and_shape_rank(tmp_path):
    memory = _memory(tmp_path)
    record_failure(memory, "Write a snake game", "shell exit 3",
                   ["shell"], ["snake.py"])
    record_failure(memory, "Bake bread", "oven cold",
                   ["shell"], ["bread.md"])
    failures = recall_failures(memory, "Write a snake game")
    assert failures and failures[0].goal == "Write a snake game"
    assert "shell exit 3" in failures[0].lesson
    assert failures[0].failed_deliverables == ["snake.py"]
    assert matching_failure(memory, "Write a snake game") is not None
    assert matching_failure(memory, "Fix the car") is None


def test_recall_experiences_excludes_failures(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    record_failure(memory, "Write a snake game", "boom",
                   ["shell"], ["snake.py"])
    recalled = recall_experiences(memory, "Write a snake game")
    assert len(recalled) == 1
    assert recalled[0].goal == "Write a snake game"
    assert recalled[0].dag_nodes


def test_finalize_records_failure_only_on_failed(tmp_path):
    memory = _memory(tmp_path)
    runner = _runner(tmp_path, memory)
    plan = Plan(steps=[{"tool": "write", "args": {"file": "snake.py"},
                        "deliverable": "snake.py"}],
                deliverables=["snake.py"], criteria=[])
    runner._finalize(RunReport("failed", "Write a snake game", plan,
                               False, False, error="boom"))
    failures = recall_failures(memory, "Write a snake game")
    assert len(failures) == 1
    assert "boom" in failures[0].lesson
    assert failures[0].failed_deliverables == ["snake.py"]
    runner._finalize(RunReport("rejected", "Write a snake game", plan,
                               False, False, error="no"))
    runner._finalize(RunReport("cancelled", "Write a snake game", plan,
                               False, False, error="stop"))
    assert len(recall_failures(memory, "Write a snake game")) == 1


# --- DAG composition ---

def test_compose_dags_sequences_and_wires(tmp_path):
    dag_a = _dag([("setup", "a.py", []), ("tests", "a_test.py", ["setup"])])
    dag_b = _dag([("setup", "b.py", []), ("tests", "b_test.py", ["setup"])])
    composed = compose_dags([dag_a, dag_b])
    assert composed is not None
    order = [node.id for node in composed.topological_order()]
    assert order.index("0:setup") < order.index("1:setup")
    assert order.index("0:tests") < order.index("1:setup")
    assert composed.nodes["1:setup"].depends_on == ["0:tests"]
    assert {n.deliverable for n in composed.nodes.values()} == {
        "a.py", "a_test.py", "b.py", "b_test.py"}


def test_compose_dags_rejects_bad_input():
    dag_a = _dag([("setup", "out.md", [])])
    dag_b = _dag([("setup", "out.md", [])])
    assert compose_dags([dag_a, dag_b]) is None
    assert compose_dags([dag_a]) is None
    assert compose_dags([]) is None
    assert compose_dags([dag_a, DAG([])]) is None


# --- loop integration ---

def test_loop_skips_adapt_when_failure_matches(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    record_failure(memory, "Write a snake game with sound",
                   "sound lib missing", ["shell"], ["snake.py"])
    client = FakeClient("")
    home = tmp_path / "home2"
    home.mkdir(exist_ok=True)
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(client, Sandbox(home / "work"), gate, home,
                        memory=memory)
    report = runner.run("Write a snake game with sound")
    assert report.adapted is False
    calls_text = " ".join(str(m.content) for call in client.calls
                          for m in call)
    assert "sound lib missing" in calls_text


def test_loop_adapts_when_no_failure_matches(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    runner = _runner(tmp_path, memory)
    report = runner.run("Write a snake game with sound")
    assert report.adapted is True
    assert report.adapted_from == "Write a snake game"


def test_loop_composes_distinct_workflows(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    store_experience(memory, _success("Write a shopping list", "list.md"))
    runner = _runner(tmp_path, memory)
    report = runner.run("Write a snake game and a shopping list")
    assert report.adapted is True
    assert "Write a snake game" in report.adapted_from
    assert "Write a shopping list" in report.adapted_from
    assert {"snake.py", "list.md"} <= set(report.plan.deliverables)
