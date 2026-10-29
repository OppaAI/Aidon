"""Tests for parallel DAG execution, parallel combine, and join steps."""

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from src.agentic.dag import (
    DAG, DAGNode, combine_dags, compose_dags, compute_levels)
from src.agentic.dag_executor import DAGExecutor
from src.agentic.loop import GoalRunner, Plan
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.memory.experience import build_experience, store_experience
from src.memory.memory import InMemoryStore, Memory
from src.system.model_client import FakeClient


def _dag(nodes):
    return DAG([DAGNode(id=n[0], tool="write", args={"file": n[1]},
                       deliverable=n[1], depends_on=n[2]) for n in nodes])


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def _runner(tmp_path, memory, client=None):
    home = tmp_path / "home2"
    home.mkdir(exist_ok=True)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(client or FakeClient(""), sandbox, gate, home,
                      memory=memory)


def _success(goal, deliverable):
    return build_experience(goal, "", [{"tool": "write",
                                       "args": {"file": deliverable,
                                                "text": f"Content for {deliverable}.\n"},
                                       "deliverable": deliverable}],
                            ["write"], [], "done", "")


# --- levels ---

def test_compute_levels_diamond_and_errors():
    assert compute_levels([("a", []), ("b", ["a"]), ("c", ["a"]),
                           ("d", ["b", "c"])]) == [["a"], ["b", "c"], ["d"]]
    assert compute_levels([("a", []), ("b", [])]) == [["a", "b"]]
    with pytest.raises(ValueError):
        compute_levels([("a", ["missing"])])
    with pytest.raises(ValueError):
        compute_levels([("a", ["b"]), ("b", ["a"])])


# --- DAGExecutor parallel ---

def test_dag_executor_parallel_barrier_and_order():
    barrier = threading.Barrier(2)
    completed = []
    lock = threading.Lock()

    def run_node(node):
        if node.id in ("b", "c"):
            # Both must enter concurrently, or the barrier times
            # out: this fails loudly under sequential execution.
            barrier.wait(timeout=5)
        with lock:
            completed.append(node.id)
        return node.id

    dag = _dag([("a", "a.md", []), ("b", "b.md", ["a"]),
                ("c", "c.md", ["a"]), ("d", "d.md", ["b", "c"])])
    result = DAGExecutor(dag, run_node).execute_parallel(max_workers=3)
    assert result["status"] == "done"
    assert set(result["completed"]) == {"a", "b", "c", "d"}
    assert completed[-1] == "d"


def test_dag_executor_parallel_failure_stops_later_levels():
    def run_node(node):
        if node.id == "b":
            raise ValueError("boom")
        return node.id

    dag = _dag([("a", "a.md", []), ("b", "b.md", ["a"]),
                ("c", "c.md", ["b"])])
    result = DAGExecutor(dag, run_node).execute_parallel()
    assert result["status"] == "failed"
    assert result["failed"] == {"b": "boom"}
    assert "c" not in result["completed"]


# --- combine ---

def test_combine_dags_parallel_no_cross_wiring_and_join():
    dag_a = _dag([("check", "a.md", [])])
    dag_b = _dag([("check", "b.md", [])])
    combined = combine_dags([dag_a, dag_b])
    assert combined is not None
    assert combined.nodes["1:check"].depends_on == []
    assert {n.deliverable for n in combined.nodes.values()} == {"a.md", "b.md"}
    join = DAGNode(id="join", tool="write", args={"file": "s.md"},
                   deliverable="s.md")
    joined = combine_dags([dag_a, dag_b], join=join)
    assert joined is not None
    assert set(joined.nodes["join"].depends_on) == {"0:check", "1:check"}
    assert combine_dags([dag_a]) is None
    assert combine_dags([dag_a, _dag([("check", "a.md", [])])]) is None
    # Sequential composition is unchanged and still available.
    assert compose_dags([dag_a, dag_b]) is not None


def test_to_plan_steps_carries_ids_and_deps():
    dag = _dag([("a", "a.md", []), ("b", "b.md", ["a"])])
    steps = dag.to_plan_steps()
    assert steps[0]["id"] == "a" and steps[0]["depends_on"] == []
    assert steps[1]["id"] == "b" and steps[1]["depends_on"] == ["a"]


# --- worker knob ---

def test_resolve_parallel_workers(monkeypatch):
    from src.agentic.dag_executor import resolve_parallel_workers
    assert resolve_parallel_workers(5) == 5
    assert resolve_parallel_workers(0) == 1
    monkeypatch.setenv("AIDON_PARALLEL_WORKERS", "7")
    assert resolve_parallel_workers() == 7
    monkeypatch.setenv("AIDON_PARALLEL_WORKERS", "bogus")
    assert resolve_parallel_workers() == 3
    monkeypatch.delenv("AIDON_PARALLEL_WORKERS", raising=False)
    assert resolve_parallel_workers() == 3


def test_settings_catalog_has_parallel_workers():
    from src.interface.webui import WebUIServer
    entries = [s for group in WebUIServer.SETTINGS_CATALOG
               for s in group["settings"]]
    match = [s for s in entries if s["key"] == "AIDON_PARALLEL_WORKERS"]
    assert len(match) == 1
    assert match[0]["type"] == "int" and match[0]["default"] == "3"


# --- loop parallel path ---

def test_loop_parallel_levels_skip_dependents_on_failure(tmp_path):
    memory = _memory(tmp_path)
    runner = _runner(tmp_path, memory)
    plan = Plan(
        steps=[
            {"id": "a", "tool": "write", "args": {"file": "a.md", "text": "A\n"},
             "deliverable": "a.md", "depends_on": []},
            {"id": "b", "tool": "shell", "args": {"command": ["false"]},
             "deliverable": "b.out", "depends_on": []},
            {"id": "c", "tool": "write", "args": {"file": "c.md", "text": "C\n"},
             "deliverable": "c.md", "depends_on": ["b"]},
            {"id": "d", "tool": "write", "args": {"file": "d.md", "text": "D\n"},
             "deliverable": "d.md", "depends_on": ["a"]},
        ],
        deliverables=["a.md", "b.out", "c.md", "d.md"], parallel_ok=True)
    passed: set[str] = set()
    failures, cancelled = runner._run_plan_levels(
        "test", plan, passed, [], 1)
    assert cancelled is False
    assert "a.md" in passed and "d.md" in passed
    assert "c.md" not in passed
    assert any("skipped" in f and "b" in f for f in failures)
    assert any("shell" in f for f in failures)


def test_loop_parallel_composed_plan_runs(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    store_experience(memory, _success("Write a shopping list", "list.md"))
    runner = _runner(tmp_path, memory)
    report = runner.run("Write a snake game and a shopping list")
    assert report.adapted is True
    assert "snake.py" in report.files and "list.md" in report.files


def test_loop_join_step_depends_on_all_sinks(tmp_path):
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    store_experience(memory, _success("Write a shopping list", "list.md"))
    join_reply = ('{"steps": [{"tool": "write", "args": {"file": "summary.md", '
                  '"text": "Combined.\\n"}, "deliverable": "summary.md"}]}')
    runner = _runner(tmp_path, memory, client=FakeClient(join_reply))
    report = runner.run("Write a snake game and a shopping list")
    assert "summary.md" in report.files
    join_steps = [s for s in report.plan.steps
                  if isinstance(s, dict) and s.get("deliverable") == "summary.md"]
    assert join_steps
    assert set(join_steps[0]["depends_on"]) == {"0:snake", "1:list"}


def test_loop_model_plan_stays_sequential(tmp_path):
    memory = _memory(tmp_path)
    runner = _runner(tmp_path, memory)
    report = runner.run("Write a note")
    assert report.status == "done"
    assert report.plan.parallel_ok is False
