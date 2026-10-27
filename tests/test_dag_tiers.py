"""DAG executor tiers and path portability."""

from src.agentic.dag import (
    DAG,
    DAGNode,
    EXECUTORS,
    relativize_paths,
    route_executor,
)

RUN_DIR = "/home/oppa-ai/.aidon/workspace/20261022-011613_abc12345"


def test_executors_constant():
    assert EXECUTORS == {"tool", "fast", "strong"}


def test_executor_default_is_tool():
    assert DAGNode(id="n1").executor == "tool"
    assert route_executor(DAGNode(id="n1")) == "tool"


def test_valid_tiers_round_trip():
    for tier in ("tool", "fast", "strong"):
        node = DAGNode.from_dict({"id": "n1", "tool": "llm", "executor": tier})
        assert node.executor == tier
        assert node.to_dict()["executor"] == tier
        assert DAGNode.from_dict(node.to_dict()).executor == tier


def test_invalid_executor_falls_back_to_tool():
    node = DAGNode.from_dict({"id": "n1", "executor": "bogus"})
    assert node.executor == "tool"
    assert route_executor({"executor": "bogus"}) == "tool"
    assert route_executor({}) == "tool"


def test_validate_reports_hand_built_bad_executor():
    dag = DAG([DAGNode(id="n1", executor="bogus")])
    failures = dag.validate()
    assert any("bogus" in failure for failure in failures)


def test_relativize_paths_rewrites_args_path():
    dag_dict = {"nodes": [
        {"id": "w", "tool": "write",
         "args": {"path": f"{RUN_DIR}/image_analysis.md"}},
    ]}
    out = relativize_paths(dag_dict, RUN_DIR)
    assert out["nodes"][0]["args"]["path"] == "image_analysis.md"


def test_relativize_paths_rewrites_path_inside_content():
    dag_dict = {"nodes": [
        {"id": "w", "tool": "write",
         "args": {"content": f"see {RUN_DIR}/image_analysis.md for details"}},
    ]}
    out = relativize_paths(dag_dict, RUN_DIR)
    assert out["nodes"][0]["args"]["content"] == "see image_analysis.md for details"


def test_relativize_paths_leaves_outside_paths_and_non_strings():
    dag_dict = {"nodes": [
        {"id": "w", "tool": "write",
         "args": {"path": "/etc/passwd", "count": 3, "flag": True,
                  "nested": {"path": "/tmp/other.txt", "n": 1}}},
    ]}
    out = relativize_paths(dag_dict, RUN_DIR)
    args = out["nodes"][0]["args"]
    assert args["path"] == "/etc/passwd"
    assert args["count"] == 3 and args["flag"] is True
    assert args["nested"]["path"] == "/tmp/other.txt"


def test_relativize_paths_does_not_mutate_input():
    dag_dict = {"nodes": [
        {"id": "w", "tool": "write",
         "args": {"path": f"{RUN_DIR}/image_analysis.md"}},
    ]}
    relativize_paths(dag_dict, RUN_DIR)
    assert dag_dict["nodes"][0]["args"]["path"] == f"{RUN_DIR}/image_analysis.md"


def test_dag_relativize_returns_new_dag():
    dag = DAG([DAGNode(id="w", tool="write",
                       args={"path": f"{RUN_DIR}/image_analysis.md"})])
    new_dag = dag.relativize(RUN_DIR)
    assert new_dag is not dag
    assert new_dag.nodes["w"].args["path"] == "image_analysis.md"
    assert dag.nodes["w"].args["path"] == f"{RUN_DIR}/image_analysis.md"


def test_to_plan_steps_carries_executor():
    dag = DAG([
        DAGNode(id="a", tool="write", deliverable="a.md", executor="tool"),
        DAGNode(id="b", tool="llm", deliverable="b.md",
                depends_on=["a"], executor="fast"),
        DAGNode(id="c", tool="llm", deliverable="c.md",
                depends_on=["b"], executor="strong"),
    ])
    steps = dag.to_plan_steps()
    assert [step["executor"] for step in steps] == ["tool", "fast", "strong"]
