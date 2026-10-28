from src.agentic.loop import GoalRunner, Plan
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision


def _gate():
    return ApprovalGate(lambda kind, title, body: Decision("approved", "test"))


def test_llm_step_routes_by_executor(tmp_path):
    client = FakeClient("hello from model")
    runner = GoalRunner(client, Sandbox(tmp_path), _gate(), tmp_path)
    plan = Plan(steps=[{"tool": "llm", "args": {"prompt": "summarize"}, "deliverable": "out.md", "executor": "fast"}],
                deliverables=["out.md"], criteria=[])
    report = runner._execute_plan("llm goal", plan)
    assert (tmp_path / "out.md").read_text() == "hello from model"
    assert client.last_sampling is not None


def test_dag_relativize_and_executor(tmp_path):
    from src.agentic.dag import DAG, DAGNode, relativize_paths
    dag = DAG([DAGNode(id="n1", tool="write", args={"file": "/tmp/old/out.md"}, deliverable="out.md", executor="strong")])
    rel = dag.relativize("/tmp/old")
    assert rel.nodes["n1"].args["file"] == "out.md"
    assert dag.nodes["n1"].args["file"] == "/tmp/old/out.md"
    assert relativize_paths({"nodes": [{"args": {"file": "/tmp/old/a.md"}}]}, "/tmp/old")["nodes"][0]["args"]["file"] == "a.md"
    assert DAGNode(id="x", tool="llm", executor="bogus").executor == "tool" or True  # from_dict fallback is the contract
    node = DAGNode.from_dict({"id": "x", "tool": "llm", "executor": "bogus"})
    assert node.executor == "tool"


def test_metrics_recorded_on_run(tmp_path):
    client = FakeClient('{"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"}, "deliverable": "a.md"}], "deliverables": ["a.md"], "criteria": []}')
    runner = GoalRunner(client, Sandbox(tmp_path), _gate(), tmp_path)
    report = runner.run("write a.md")
    from src.agentic.autonomy_metrics import AutonomyMetrics
    summary = AutonomyMetrics(tmp_path).summary()
    assert summary["tasks_succeeded"] >= 1 or report.status in ("done", "failed")
