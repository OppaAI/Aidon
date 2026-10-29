"""AIDON_DAG_SIMILARITY: one tunable knob for "same goal shape".

Boundary goals (Jaccard on content words, stopwords dropped):
- "alpha bravo charlie delta" vs "alpha bravo echo foxtrot":
  2 shared / 6 union = 1/3, so default 0.3 matches, 0.5 does not.
- "alpha bravo charlie delta echo" vs
  "alpha bravo foxtrot golf hotel india juliett":
  2 shared / 10 union = 0.2, so default 0.3 does not match,
  0.2 does.
- "alpha bravo charlie delta" vs "alpha golf hotel india":
  1 shared / 7 union, so default 0.3 does not match, 0.1 does.
"""

from types import SimpleNamespace

import pytest

from src.agentic.adapt import adapt_experience
from src.agentic.dag import adapt_dag
from src.agentic.dag_store import DagStore
from src.agentic.loop import GoalRunner
from src.interface.webui import WebUIServer
from src.memory.experience import (
    build_experience,
    dag_similarity_threshold,
    matching_failure,
)

GOAL_A = "alpha bravo charlie delta"
GOAL_B = "alpha bravo echo foxtrot"
GOAL_C = "alpha bravo charlie delta echo"
GOAL_D = "alpha bravo foxtrot golf hotel india juliett"
GOAL_E = "alpha golf hotel india"

NODES = [{"tool": "write", "args": {"path": "out.txt", "content": "hi"},
          "deliverable": "out.txt"}]


@pytest.mark.parametrize("raw,expected", [
    (None, 0.3),
    ("", 0.3),
    ("0.5", 0.5),
    (" 0.4 ", 0.4),
    ("abc", 0.3),
    ("2", 1.0),
    ("-0.5", 0.0),
    ("nan", 0.3),
    ("inf", 0.3),
])
def test_resolver_defaults_clamps_and_rejects_garbage(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    else:
        monkeypatch.setenv("AIDON_DAG_SIMILARITY", raw)
    assert dag_similarity_threshold() == expected


def test_learn_replaces_at_default_but_adds_at_05(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    store = DagStore(tmp_path / "home1")
    status, first = store.learn(GOAL_A, NODES)
    assert status == "added"
    status, second = store.learn(GOAL_B, NODES)
    assert (status, second) == ("replaced", first)

    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.5")
    store2 = DagStore(tmp_path / "home2")
    status, first2 = store2.learn(GOAL_A, NODES)
    assert status == "added"
    status, second2 = store2.learn(GOAL_B, NODES)
    assert status == "added" and second2 != first2


def test_learn_boundary_02(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    store = DagStore(tmp_path / "home1")
    store.learn(GOAL_C, NODES)
    status, _ = store.learn(GOAL_D, NODES)
    assert status == "added"

    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.2")
    store2 = DagStore(tmp_path / "home2")
    _, first2 = store2.learn(GOAL_C, NODES)
    status, second2 = store2.learn(GOAL_D, NODES)
    assert (status, second2) == ("replaced", first2)


def _experience(goal):
    return build_experience(goal, "", NODES, ["write"], [], "done")


def test_adapt_experience_follows_threshold(monkeypatch):
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    assert adapt_experience(_experience(GOAL_A), GOAL_B, client=None) is not None
    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.5")
    assert adapt_experience(_experience(GOAL_A), GOAL_B, client=None) is None


def test_adapt_dag_follows_threshold(monkeypatch):
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    result = adapt_dag(_experience(GOAL_A), GOAL_B, client=None)
    assert result is not None and result[2] == 0
    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.5")
    assert adapt_dag(_experience(GOAL_A), GOAL_B, client=None) is None


class _StubMemory:
    def __init__(self, hits):
        self._hits = hits

    def recall(self, *args, **kwargs):
        return self._hits


def _failure_hit():
    return SimpleNamespace(point=SimpleNamespace(
        payload={"kind": "failed_example", "goal": GOAL_B,
                 "goal_shape": "alpha bravo echo foxtrot",
                 "outcome": "failed: boom", "tools_used": ["write"],
                 "failed_deliverables": [], "lesson": ""},
        text=GOAL_B))


def test_matching_failure_follows_threshold(monkeypatch):
    memory = _StubMemory([_failure_hit()])
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    assert matching_failure(memory, GOAL_A) is not None
    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.5")
    assert matching_failure(memory, GOAL_A) is None


def _composed_runner(tmp_path):
    runner = GoalRunner.__new__(GoalRunner)
    runner._propose_join_step = lambda goal, dags: None
    runner._research_and_replan = lambda plan, goal: plan
    runner.sandbox = SimpleNamespace(workdir=tmp_path)
    return runner


def _candidate(goal, node_id, deliverable):
    return SimpleNamespace(
        goal=goal, goal_shape="",
        dag_nodes=[{"id": node_id, "tool": "write",
                    "args": {"path": deliverable, "content": "x"},
                    "deliverable": deliverable}])


def test_composed_plan_candidate_filter_follows_threshold(tmp_path, monkeypatch):
    runner = _composed_runner(tmp_path)
    near = _candidate(GOAL_B, "b1", "b.txt")
    far = _candidate(GOAL_E, "e1", "e.txt")
    monkeypatch.delenv("AIDON_DAG_SIMILARITY", raising=False)
    assert runner._composed_plan([near, far], GOAL_A) is None
    monkeypatch.setenv("AIDON_DAG_SIMILARITY", "0.1")
    result = runner._composed_plan([near, far], GOAL_A)
    assert result is not None
    plan, goals = result
    assert goals == [GOAL_B, GOAL_E]
    assert set(plan.deliverables) == {"b.txt", "e.txt"}


def test_settings_catalog_carries_threshold(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    server = WebUIServer(home)
    groups = {g["group"]: g["settings"]
              for g in server.settings_catalog()["groups"]}
    entry = [e for e in groups["Memory"] if e["key"] == "AIDON_DAG_SIMILARITY"]
    assert len(entry) == 1 and entry[0]["default"] == "0.3"
    assert "AIDON_DAG_SIMILARITY" in WebUIServer._CATALOG_FLOAT_KEYS
    assert server.settings_set_env("AIDON_DAG_SIMILARITY", "0.5")["ok"] is True
    assert server.settings_set_env("AIDON_DAG_SIMILARITY", "abc")["ok"] is False
