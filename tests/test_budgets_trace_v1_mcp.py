"""Budgets, trace, v1 API, token auth, MCP, in one shot.

Keys are stored as hashes only, never as real keys.
Agent tokens cannot approve or manage keys, in v1 or
in MCP. Budgets fail a run, they do not silently
continue it.
"""

import json

from src.approval import ApprovalGate, Decision
from src.budgets import Budget, BudgetExceeded
from src.loop import GoalRunner
from src.mcp_server import MCPServer
from src.model_client import FakeClient
from src.sandbox import Sandbox
from src.trace import TraceRecorder
from src.v1 import AuthStore, GoalStore, handle


def test_budget_step_limit_fails_run(tmp_path):
    home = tmp_path / "home"
    plan = {"steps": [{"tool": "write", "args": {"file": "note.md", "text": "done"}, "deliverable": "note.md"}],
            "deliverables": ["note.md"], "criteria": []}
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    budget = Budget(max_steps=0)
    runner = GoalRunner(FakeClient(json.dumps(plan)), Sandbox(home / "work"),
                        gate, home, memory=None, budget=budget)
    report = runner.run("Write a note")
    assert report.status == "failed"
    assert "budget" in report.error.lower() or "step" in report.error.lower()


def test_trace_events_in_order(tmp_path):
    home = tmp_path / "home"
    plan = {"steps": [{"tool": "write", "args": {"file": "note.md", "text": "done"}, "deliverable": "note.md"}],
            "deliverables": ["note.md"], "criteria": []}
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    trace = TraceRecorder(home, project_id="test")
    runner = GoalRunner(FakeClient(json.dumps(plan)), Sandbox(home / "work"),
                        gate, home, memory=None, trace=trace)
    report = runner.run("Write a note")
    assert report.status == "done"
    kinds = trace.kinds()
    assert "plan" in kinds and "sandbox" in kinds and "verify" in kinds
    assert "approval" in kinds and "learn" in kinds
    assert (home / "trace" / f"{trace.run_id}.jsonl").exists()


def test_v1_token_auth_and_scopes(tmp_path):
    home = tmp_path / "home"
    auth = AuthStore(home)
    goals = GoalStore(home)
    owner_key, _ = auth.create_key("owner", "owner")
    agent_key, _ = auth.create_key("agent1", "agent")
    # Real key is not stored, only its hash.
    stored = (home / "v1" / "keys.json").read_text()
    assert owner_key not in stored and agent_key not in stored
    # 401 missing, 401 invalid.
    assert handle(auth, goals, "GET", "/api/v1/goals", {}, {})[0] == 401
    assert handle(auth, goals, "GET", "/api/v1/goals", {"Authorization": "Bearer bad"}, {})[0] == 401
    # Health is liveness only, no auth.
    assert handle(auth, goals, "GET", "/api/v1/health", {}, {})[0] == 200
    # Agent submits, owner reads, agent cannot approve or manage keys.
    status, payload = handle(auth, goals, "POST", "/api/v1/goals",
                             {"Authorization": f"Bearer {agent_key}"},
                             {"goal": "Do a thing"})
    assert status == 201
    goal_id = payload["id"]
    assert handle(auth, goals, "POST", f"/api/v1/goals/{goal_id}/approve",
                  {"Authorization": f"Bearer {agent_key}"}, {})[0] == 403
    assert handle(auth, goals, "GET", "/api/v1/keys",
                  {"Authorization": f"Bearer {agent_key}"}, {})[0] == 403
    assert handle(auth, goals, "POST", f"/api/v1/goals/{goal_id}/approve",
                  {"Authorization": f"Bearer {owner_key}"}, {})[0] == 200
    assert handle(auth, goals, "GET", "/api/v1/keys",
                  {"Authorization": f"Bearer {owner_key}"}, {})[0] == 200


def test_mcp_tools_filtered_by_scope(tmp_path):
    home = tmp_path / "home"
    auth = AuthStore(home)
    goals = GoalStore(home)
    owner_key, _ = auth.create_key("owner", "owner")
    agent_key, _ = auth.create_key("agent1", "agent")
    server = MCPServer(auth, goals)
    owner_tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, owner_key)
    agent_tools = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, agent_key)
    owner_names = {t["name"] for t in owner_tools["result"]["tools"]}
    agent_names = {t["name"] for t in agent_tools["result"]["tools"]}
    assert "approve_goal" in owner_names and "list_keys" in owner_names
    assert "approve_goal" not in agent_names and "list_keys" not in agent_names
    # Agent cannot call approve, even if it names the tool.
    result = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                            "params": {"name": "approve_goal", "arguments": {"id": "x"}}}, agent_key)
    assert "error" in result
    # Agent can submit and read its own goal.
    submitted = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                               "params": {"name": "submit_goal", "arguments": {"goal": "MCP goal"}}}, agent_key)
    assert "result" in submitted
