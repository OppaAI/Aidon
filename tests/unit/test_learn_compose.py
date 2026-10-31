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


class _PassingSkillSandbox:
    def run(self, command):
        return {"ok": True, "output": "pass"}


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


def test_dag_from_experience_restores_dependencies(tmp_path):
    """Stored dependencies survive a save/recall round trip.

    A workflow whose steps were parallel must not come back
    as a sequential chain: nodes stored with depends_on
    keep it, while nodes stored without it (older records)
    still fall back to the chain.
    """
    from src.agentic.dag import dag_from_experience
    exp = build_experience("Write a snake game", "", [
        {"id": "a", "tool": "write", "args": {},
         "deliverable": "a.py", "depends_on": [],
         "executor": "tool", "expected": "runs"},
        {"id": "b", "tool": "write", "args": {},
         "deliverable": "b.py", "depends_on": ["a"],
         "executor": "tool", "expected": "imports a"},
        {"id": "c", "tool": "write", "args": {},
         "deliverable": "c.py", "depends_on": [],
         "executor": "strong", "expected": "standalone"},
    ], ["write"], [], "done", "")
    dag = dag_from_experience(exp)
    by_id = {node.id: node for node in dag.nodes.values()}
    assert by_id["a"].depends_on == []
    assert by_id["b"].depends_on == ["a"]
    assert by_id["c"].depends_on == []
    assert by_id["c"].executor == "strong"
    assert by_id["a"].expected == "runs"
    old = build_experience("Write a snake game", "", [
        {"tool": "write", "args": {}, "deliverable": "a.py"},
        {"tool": "write", "args": {}, "deliverable": "b.py"},
    ], ["write"], [], "done", "")
    old_dag = dag_from_experience(old)
    ids = list(old_dag.nodes.values())
    assert ids[0].depends_on == []
    assert ids[1].depends_on == [ids[0].id]


def _experience_points(memory):
    return list(memory.iter_points("experience"))


def test_merge_refuses_same_deliverable_different_args(tmp_path):
    """Same name, different procedure: kept separate, not merged.

    Merging would silently keep the old steps and drop the
    new ones, so the two experiences stay as two points.
    """
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    changed = build_experience("Write a snake game", "", [{
        "tool": "write",
        "args": {"file": "snake.py", "text": "Totally different content.\n"},
        "deliverable": "snake.py"}], ["write"], [], "done", "")
    store_experience(memory, changed)
    assert len(_experience_points(memory)) == 2


def test_merge_validates_merged_dag(tmp_path):
    """A merged node set that fails DAG validation is not saved."""
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    broken = build_experience("Write a snake game", "", [{
        "id": "x", "tool": "write", "args": {},
        "deliverable": "other.py", "depends_on": ["missing-id"],
    }], ["write"], [], "done", "")
    store_experience(memory, broken)
    assert len(_experience_points(memory)) == 2


def test_merge_still_works_for_compatible_workflows(tmp_path):
    """Compatible workflows still union into one point."""
    memory = _memory(tmp_path)
    store_experience(memory, _success("Write a snake game", "snake.py"))
    extra = build_experience("Write a snake game", "", [{
        "tool": "write",
        "args": {"file": "tests.py", "text": "Content for tests.py.\n"},
        "deliverable": "tests.py"}], ["write"], [], "done", "")
    store_experience(memory, extra)
    points = _experience_points(memory)
    assert len(points) == 1
    import json
    nodes = json.loads(points[0].payload["dag_nodes"])
    assert {n["deliverable"] for n in nodes} == {"snake.py", "tests.py"}


def test_experience_promotes_to_discoverable_skill(tmp_path):
    """A promoted workflow lands as a skill draft file.

    The file exists with the workflow text, but it does
    not enter run context on its own: the plan prompt
    carries trusted skills only. Adding it to the library
    and trusting it publishes it into discovery, and
    only then do the plugin registry's skill texts pick
    it up.
    """
    from src.capabilities.plugins import PluginRegistry
    from src.capabilities.skill_library import Skill, SkillLibrary
    from src.memory.experience import (
        promote_experience_to_skill, render_experience_skill)
    memory = _memory(tmp_path)
    exp = _success("Write a snake game", "snake.py")
    text = render_experience_skill(exp)
    assert "Write a snake game" in text
    assert "snake.py" in text
    path = promote_experience_to_skill(tmp_path / "home", exp)
    assert path.exists()
    assert path.read_text(encoding="utf-8") == text
    texts = PluginRegistry(tmp_path / "home").skill_texts()
    assert not any("Write a snake game" in value for value in texts.values())
    lib = SkillLibrary(tmp_path / "home")
    lib.add(Skill(name=path.stem, description="Snake game workflow",
                  test_commands=["python3 --version"]))
    lib.promote(path.stem, lib.run_tests(path.stem, _PassingSkillSandbox()))
    lib.trust(path.stem, approved=True)
    texts = PluginRegistry(tmp_path / "home").skill_texts()
    assert path.name in texts
    try:
        promote_experience_to_skill(tmp_path / "home", exp)
    except FileExistsError:
        pass
    else:
        raise AssertionError("promotion must not overwrite an existing skill")


def test_trust_publishes_and_retire_removes_discovery(tmp_path):
    """Only trusted library skills reach the planner; retired ones leave.

    The JSON library is not what the planner reads, so trust
    publishes the skill's Markdown into <home>/skills/ and
    retire removes it again. Candidates are never published.
    """
    from src.capabilities.skill_library import Skill, SkillLibrary
    lib = SkillLibrary(tmp_path / "home")
    lib.add(Skill(name="greeter", description="Says hello.",
                  test_commands=["python3 --version"]))
    assert not (tmp_path / "home" / "skills" / "greeter.md").exists()
    lib.promote("greeter", lib.run_tests("greeter", _PassingSkillSandbox()))
    lib.trust("greeter", approved=True)
    published = tmp_path / "home" / "skills" / "greeter.md"
    assert published.exists()
    assert "Says hello." in published.read_text(encoding="utf-8")
    lib.retire("greeter", "no longer needed")
    assert not published.exists()
