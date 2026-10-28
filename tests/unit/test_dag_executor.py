from src.agentic.dag import DAG, DAGNode
from src.agentic.dag_executor import DAGExecutor
from src.capabilities.registry_db import Registry


def _dag():
    return DAG([
        DAGNode(id="a", tool="write", deliverable="a.md"),
        DAGNode(id="b", tool="write", deliverable="b.md", depends_on=["a"]),
        DAGNode(id="c", tool="write", deliverable="c.md", depends_on=["b"]),
    ])


def test_topological_order_execution():
    calls = []

    def run_node(node):
        calls.append(node.id)
        return f"done-{node.id}"

    result = DAGExecutor(_dag(), run_node).execute()
    assert calls == ["a", "b", "c"]
    assert result["status"] == "done"
    assert result["results"] == {"a": "done-a", "b": "done-b", "c": "done-c"}
    assert result["completed"] == ["a", "b", "c"]
    assert result["failed"] is None


def test_registry_state_kv_roundtrip(tmp_path):
    reg = Registry(tmp_path)
    assert reg.get_state("run1", "plan") == ""
    reg.save_state("run1", "plan", '{"a": 1}')
    assert reg.get_state("run1", "plan") == '{"a": 1}'
    reg.save_state("run1", "plan", '{"a": 2}')
    assert reg.get_state("run1", "plan") == '{"a": 2}'
    reg.save_state("run2", "plan", "run2-plan")
    assert reg.get_state("run1", "plan") == '{"a": 2}'
    reg.clear_state("run1")
    assert reg.get_state("run1", "plan") == ""
    assert reg.get_state("run2", "plan") == "run2-plan"


def test_resume_skips_completed_nodes(tmp_path):
    reg = Registry(tmp_path)
    calls = []
    fail_at = {"b"}

    def run_node(node):
        calls.append(node.id)
        if node.id in fail_at:
            raise RuntimeError("boom")
        return f"done-{node.id}"

    first = DAGExecutor(_dag(), run_node, state=reg, run_id="run1").execute()
    assert first["status"] == "failed"
    assert first["completed"] == ["a"]
    assert first["results"] == {"a": "done-a"}
    # "Crash" and resume: the failing node now succeeds, node a is skipped.
    fail_at.clear()
    calls.clear()
    second = DAGExecutor(_dag(), run_node, state=reg, run_id="run1").resume()
    assert calls == ["b", "c"]
    assert second["status"] == "done"
    assert second["results"] == {"a": "done-a", "b": "done-b", "c": "done-c"}


def test_transient_failure_retried_once_then_succeeds():
    attempts = {"n": 0}

    def run_node(node):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("transient network blip")
        return "ok"

    dag = DAG([DAGNode(id="only", tool="write", deliverable="o.md")])
    result = DAGExecutor(dag, run_node).execute()
    assert result["status"] == "done"
    assert attempts["n"] == 2


def test_transient_failure_twice_fails_run():
    def run_node(node):
        raise RuntimeError("transient again")

    dag = DAG([DAGNode(id="only", tool="write", deliverable="o.md")])
    result = DAGExecutor(dag, run_node).execute()
    assert result["status"] == "failed"
    assert result["failed"] == {"only": "transient again"}


def test_real_failure_preserves_completed_results(tmp_path):
    reg = Registry(tmp_path)
    calls = []

    def run_node(node):
        calls.append(node.id)
        if node.id == "b":
            raise ValueError("bad input")
        return f"done-{node.id}"

    executor = DAGExecutor(_dag(), run_node, state=reg, run_id="run9")
    result = executor.execute()
    assert result["status"] == "failed"
    assert result["failed"] == {"b": "bad input"}
    assert result["completed"] == ["a"]
    assert result["results"] == {"a": "done-a"}
    assert calls == ["a", "b"]
    # Checkpoints survive the failure: partial results are still there.
    assert executor.partial_results() == {"a": "done-a"}
    assert reg.get_state("run9", "node:a") == "done-a"
    assert reg.get_state("run9", "node:b") == ""


def test_partial_results_content():
    executor = DAGExecutor(_dag(), lambda node: f"done-{node.id}", state=None)
    assert executor.partial_results() == {}
    executor.execute()
    assert executor.partial_results() == {"a": "done-a", "b": "done-b", "c": "done-c"}


def test_checkpoint_is_authority_not_files(tmp_path):
    # A deliverable file existing on disk does not skip a node;
    # only a checkpoint does.
    (tmp_path / "a.md").write_text("stale")
    calls = []

    def run_node(node):
        calls.append(node.id)
        return f"done-{node.id}"

    result = DAGExecutor(_dag(), run_node).execute()
    assert calls == ["a", "b", "c"]
    assert result["status"] == "done"


def test_different_dag_with_same_run_id_does_not_reuse_nodes(tmp_path):
    reg = Registry(tmp_path)
    calls = []
    first = DAGExecutor(_dag(), lambda node: calls.append(node.id) or "first",
                        state=reg, run_id="same")
    assert first.execute()["status"] == "done"
    calls.clear()
    changed = DAG([DAGNode(id="a", tool="read", deliverable="a.md")])
    second = DAGExecutor(changed, lambda node: calls.append(node.id) or "second",
                         state=reg, run_id="same")
    result = second.execute()
    assert result["results"] == {"a": "second"}
    assert calls == ["a"]
    reg.close()


def test_different_dag_clears_stale_node_ids(tmp_path):
    reg = Registry(tmp_path)
    DAGExecutor(_dag(), lambda node: f"first-{node.id}",
                state=reg, run_id="same").execute()
    changed = DAG([
        DAGNode(id="a", tool="read", deliverable="a.md"),
        DAGNode(id="b", tool="read", deliverable="b.md", depends_on=["a"]),
    ])
    result = DAGExecutor(changed, lambda node: f"second-{node.id}",
                         state=reg, run_id="same").execute()
    assert result["results"] == {"a": "second-a", "b": "second-b"}
    reg.close()
