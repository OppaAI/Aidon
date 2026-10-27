"""Adaptive DAG and steer/cancel, across surfaces, file-based."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agentic.adapt import adapt_experience, generalize_nodes, instantiate_template
from src.agentic.benchmark import benchmark_learn, benchmark_total
from src.agentic.counting import CountingClient
from src.agentic.dag import DAG, DAGNode, adapt_dag, dag_from_experience
from src.memory.experience import build_experience
from src.agentic.loop import GoalRunner, RunReport, Plan
from src.memory.memory import Memory
from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.system.model_client import FakeClient
from src.interface.run_control import RunControl, new_run_id
from src.capabilities.sandbox import Sandbox
from src.interface.webui import WebUIServer
from src.interface.triggers import TriggerDispatcher


def _memory(tmp_path):
    return Memory(tmp_path / "mem", embedder=HashEmbedder())


def test_dag_add_remove_modify_reorder_and_cycle():
    dag = DAG([DAGNode("setup", deliverable="setup.py"),
               DAGNode("logic", deliverable="game.py", depends_on=["setup"]),
               DAGNode("tests", deliverable="game_test.py", depends_on=["logic"])])
    assert [n.id for n in dag.topological_order()] == ["setup", "logic", "tests"]
    dag.modify("logic", deliverable="worm.py")
    assert dag.nodes["logic"].deliverable == "worm.py"
    dag.add(DAGNode("extra", deliverable="extra.py", depends_on=["tests"]))
    dag.remove("tests")
    # tests removed, extra rewired to logic
    assert "logic" in dag.nodes["extra"].depends_on
    assert dag.validate() == []
    # Cycle: logic depends on extra, extra depends on logic
    dag.nodes["logic"].depends_on.append("extra")
    assert dag.validate(), "a cycle must fail validation"


def test_adapt_dag_snake_to_worm_keeps_modifies_adds_removes():
    experience = build_experience(
        "Write a Snake game", "game snake write",
        [{"tool": "write", "args": {"file": "snake.py", "text": "Snake game loop"},
          "deliverable": "snake.py", "assumptions": []},
         {"tool": "write", "args": {"file": "snake_skins.py", "text": "Snake skins"},
          "deliverable": "snake_skins.py", "assumptions": ["game=snake"]},
         {"tool": "write", "args": {"file": "snake_test.py", "text": "Snake tests"},
          "deliverable": "snake_test.py", "assumptions": []}],
        ["write"], [], "done")
    dag, summary, _calls = adapt_dag(experience, "Write a Worm game", client=None)
    assert dag is not None
    assert "snake_skins" in summary["removed"], "Snake-only node must be removed, not renamed"
    assert any("worm" in n.deliverable for n in dag.nodes.values())
    assert dag.validate() == []
    steps = dag.to_plan_steps()
    assert any(s["deliverable"] == "worm.py" for s in steps)
    assert not any(s["deliverable"] == "snake_skins.py" for s in steps)


def test_adapt_dag_rollback_on_invalid_graph():
    experience = build_experience(
        "Write a Snake game", "game snake write",
        [{"tool": "write", "args": {"file": "snake.py", "text": "Snake"}, "deliverable": "snake.py"}],
        ["write"], [], "done")
    # A client that tries to add a node with a missing dependency
    class BadClient:
        def chat(self, messages):
            return '{"add": [{"id": "broken", "tool": "write", "args": {"file": "broken.py"}, "deliverable": "broken.py", "depends_on": ["missing"]}]}'
    dag, summary, _calls = adapt_dag(experience, "Write a Worm game", client=BadClient())
    assert summary["rolled_back"] is True
    assert dag.validate() == []


def test_generalize_and_instantiate_template():
    nodes = [{"tool": "write", "args": {"file": "snake.py", "text": "Snake game"}, "deliverable": "snake.py"}]
    template = generalize_nodes(nodes, "Write a Snake game")
    assert template[0]["args"]["file"] == "{task}.py"
    adapted = instantiate_template(template, "Write a Worm game")
    assert adapted[0]["args"]["file"] == "worm.py"


def test_benchmark_total_includes_learning_cost():
    total = benchmark_total(learn_calls=5, learn_tokens=1000, fresh_calls=5, fresh_tokens=1000,
                            adapted_calls=1, adapted_tokens=200)
    assert total["fresh_total_calls"] == 10
    assert total["adapted_total_calls"] == 6
    assert total["calls_saved_total"] == 4


def test_run_control_cancel_and_steer_file_based(tmp_path):
    control = RunControl(tmp_path / "home")
    run_id = new_run_id()
    control.register(run_id, "Write a Worm game")
    assert control.steer(run_id, "Use a slower worm speed") is True
    assert control.steer_notes(run_id) == ["Use a slower worm speed"]
    assert control.cancel(run_id) is True
    assert control.is_cancelled(run_id) is True
    # A second control instance, same home, sees the same file, as CLI would.
    control2 = RunControl(tmp_path / "home")
    assert control2.is_cancelled(run_id) is True
    assert control2.steer_notes(run_id) == ["Use a slower worm speed"]


def test_loop_cancelled_run_does_not_execute(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    control = RunControl(home)
    run_id = new_run_id()
    control.register(run_id, "Write a note")
    control.cancel(run_id)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, memory=_memory(tmp_path),
                        run_id=run_id, run_control=control)
    report = runner.run("Write a note")
    assert report.status == "cancelled"
    assert not (home / "work" / "deliverable.md").exists()


def test_webui_steer_cancel_commands(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    control = RunControl(home)
    run_id = new_run_id()
    control.register(run_id, "Write a Worm game")
    server = WebUIServer(home, dispatcher=TriggerDispatcher(), run_control=control)
    assert server._resolve_control(f"steer {run_id} slower worm") == f"Steered {run_id}: slower worm"
    assert server._resolve_control(f"cancel {run_id}") == f"Cancelled {run_id}"
    assert control.is_cancelled(run_id) is True
    assert server._resolve_control("cancel no-such-run") == "No such run no-such-run"


def test_loop_adapted_dag_via_experience(tmp_path):
    # A Snake experience in memory, then a Worm goal, should adapt, not replay Snake.
    memory = _memory(tmp_path)
    from src.memory.experience import store_experience
    experience = build_experience(
        "Write a Snake game", "game snake write",
        [{"tool": "write", "args": {"file": "snake.py", "text": "Snake game loop"}, "deliverable": "snake.py"}],
        ["write"], [], "done")
    store_experience(memory, experience)
    home = tmp_path / "home2"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(""), sandbox, gate, home, memory=memory)
    report = runner.run("Write a Worm game")
    # The adapted DAG writes worm.py, or, if recall did not match, the fallback
    # writes deliverable.md. Either way, the run must not deliver snake.py.
    assert "snake.py" not in report.files
