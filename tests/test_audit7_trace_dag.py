"""Seventh-audit fixes, part B: trace attribution + DAG validation.

Trace: record() is lock-guarded and accepts an optional run_id
override, so parallel sub-runs sharing one recorder no longer
interleave lines or misattribute events to the parent run.
DAG: __init__ still keeps the last node per id, but duplicate
ids are recorded and validate() reports them; validate_strict()
raises. Executor tiers are intentionally untouched (see the
last test): tool/fast/strong routing is a designed feature.
"""
import json
import threading

import pytest

from src.agentic.dag import DAG, DAGNode, route_executor
from src.agentic.trace import TraceRecorder


def _read_events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_record_default_unchanged(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    event = rec.record("plan", "done", "hello")
    assert event.run_id == "parent"
    assert rec.kinds() == ["plan"]
    events = _read_events(tmp_path / "trace" / "parent.jsonl")
    assert len(events) == 1 and events[0]["run_id"] == "parent"


def test_record_run_id_override_attributes_to_subrun(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    event = rec.record("sandbox", "done", "sub work", run_id="child-1")
    assert event.run_id == "child-1"
    # The sub-run's event lands in the sub-run's file, not the parent's.
    assert _read_events(tmp_path / "trace" / "child-1.jsonl")[0]["run_id"] == "child-1"
    assert _read_events(tmp_path / "trace" / "parent.jsonl") == []
    # And the shared in-memory list still holds it.
    assert rec.events[-1].run_id == "child-1"


def test_record_parallel_threads_no_lost_or_corrupt_lines(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    threads = [
        threading.Thread(target=lambda i=i: [
            rec.record("sandbox", "done", f"step {i}-{j}", run_id=f"sub-{i}")
            for j in range(25)])
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(rec.events) == 200
    for i in range(8):
        events = _read_events(tmp_path / "trace" / f"sub-{i}.jsonl")
        assert len(events) == 25
        assert all(e["run_id"] == f"sub-{i}" for e in events)


def test_kinds_snapshot_under_lock(tmp_path):
    rec = TraceRecorder(tmp_path, run_id="parent")
    rec.record("plan", "done")
    rec.record("sandbox", "done")
    assert rec.kinds() == ["plan", "sandbox"]


def test_dag_init_still_keeps_last_node_but_flags_duplicate():
    first = DAGNode(id="a", tool="write", args={"file": "one.py"})
    second = DAGNode(id="a", tool="write", args={"file": "two.py"})
    dag = DAG([first, second])
    # __init__ behaviour unchanged: one entry, last wins.
    assert list(dag.nodes) == ["a"]
    assert dag.nodes["a"].args == {"file": "two.py"}
    # But validation no longer stays silent about it.
    assert any("duplicate node id: a" in f for f in dag.validate())


def test_dag_validate_strict_raises_on_duplicate():
    dag = DAG([DAGNode(id="a"), DAGNode(id="a", tool="read")])
    with pytest.raises(ValueError, match="duplicate node id: a"):
        dag.validate_strict()


def test_dag_validate_strict_passes_clean_and_cycle_still_loud():
    clean = DAG([DAGNode(id="a"), DAGNode(id="b", depends_on=["a"])])
    assert clean.validate() == []
    assert clean.validate_strict() is None
    cyclic = DAG([DAGNode(id="a", depends_on=["b"]),
                  DAGNode(id="b", depends_on=["a"])])
    assert any("cycle" in f for f in cyclic.validate())
    with pytest.raises(ValueError, match="cycle"):
        cyclic.validate_strict()


def test_dag_add_still_raises_on_duplicate():
    dag = DAG([DAGNode(id="a")])
    with pytest.raises(ValueError, match="duplicate node id"):
        dag.add(DAGNode(id="a"))


def test_executor_tiers_untouched():
    # Deliberately not a gap: tool/fast/strong is a designed tier
    # feature, and `dag run` refusing non-tool nodes does not
    # change how the loop routes them. This test pins the tiers.
    assert route_executor(DAGNode(id="a", executor="fast")) == "fast"
    assert route_executor(DAGNode(id="b", executor="strong")) == "strong"
    assert route_executor(DAGNode(id="c", executor="tool")) == "tool"
    assert route_executor(DAGNode(id="d", executor="bogus")) == "tool"
