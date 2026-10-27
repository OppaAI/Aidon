"""Sixth-audit fixes, part A: main.py branches only.

Covers what can be unit-tested without running the CLI: the CLI gate
timeout threading, declare_blocked's direction-backed string shape, and
the dag run wiring (executor + tool registry + Registry checkpoints)
that `aidon dag run` uses. The serve-project, interview-runner, and
swarm-runner branch edits are verified by inspection of the branch
code (they live inside main() and need a full CLI run to execute);
the assertions below pin the helpers those branches depend on.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agentic.dag import DAG, DAGNode
from src.agentic.dag_executor import DAGExecutor
from src.agentic.direction import blocked_note
from src.capabilities.registry_db import Registry
from src.capabilities.tools import ToolRegistry
from src.system.main import _cli_gate, _goal_extra_tools


class _Config:
    github_token = ""
    threads_account_id = ""
    threads_access_token = ""
    ig_user_id = ""
    ig_access_token = ""
    fish_api_key = ""
    tts_voice_id = ""
    transcribe_api_key = ""
    owner_email = "owner@example.com"


def _tools(tmp_path, **kw):
    cfg = _Config()
    cfg.home = tmp_path
    return _goal_extra_tools(cfg, tmp_path / "work", **kw)


def test_cli_gate_default_timeout_is_180(tmp_path):
    assert _cli_gate(tmp_path).timeout_seconds == 180


def test_cli_gate_accepts_configured_timeout():
    assert _cli_gate(42).timeout_seconds == 42


def test_declare_blocked_keeps_exact_string_shape():
    tools = _tools(Path("/tmp/audit6-test-home"))
    assert tools["declare_blocked"]("no network") == "[blocked] no network"


def test_declare_blocked_matches_direction_blocked_note():
    tools = _tools(Path("/tmp/audit6-test-home"))
    for reason in ("stuck", "", "needs a key"):
        assert tools["declare_blocked"](reason) == f"[blocked] {blocked_note(reason)['reason']}"


def test_dag_run_tool_nodes_execute_in_order(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "hello.txt", "text": "hi"}),
        DAGNode(id="b", tool="read", args={"file": "hello.txt"}, depends_on=["a"]),
    ])
    result = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args)).execute()
    assert result["status"] == "done"
    assert result["completed"] == ["a", "b"]
    assert result["results"]["b"] == "hi"


def test_dag_run_unknown_tool_fails_without_rollback(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "kept.txt", "text": "kept"}),
        DAGNode(id="b", tool="nope", args={}, depends_on=["a"]),
    ])
    result = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args)).execute()
    assert result["status"] == "failed"
    assert result["completed"] == ["a"]
    assert (workdir / "kept.txt").read_text() == "kept"


def test_dag_run_resume_skips_checkpointed_nodes(tmp_path):
    workdir = tmp_path / "work"
    registry = ToolRegistry(workdir, sandbox=None)
    dag = DAG([
        DAGNode(id="a", tool="write", args={"file": "x.txt", "text": "x"}),
        DAGNode(id="b", tool="write", args={"file": "y.txt", "text": "y"}, depends_on=["a"]),
    ])
    state = Registry(tmp_path / "home")
    first = DAGExecutor(dag, lambda node: registry.call(node.tool, node.args),
                        state=state, run_id="resume-1")
    # Simulate a killed run: checkpoint only node a, then resume.
    first._set_checkpoint("a", "wrote x.txt")
    calls = []
    def counting(node):
        calls.append(node.id)
        return registry.call(node.tool, node.args)
    resumed = DAGExecutor(dag, counting, state=state, run_id="resume-1").execute()
    assert resumed["status"] == "done"
    assert calls == ["b"]
    assert resumed["completed"] == ["a", "b"]
