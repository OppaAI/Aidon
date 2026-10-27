"""MCP server wrapping the v1 API, stdio JSON-RPC, scope-filtered.

MCP is not a separate API with separate logic. MCP
tools call the same GoalStore and AuthStore as the
v1 API, with the same scope rules, so a tool an
agent token cannot use in v1 cannot be used in MCP
either, and a tool list for an agent token never
includes approve or key management tools.

Protocol, minimal and testable:
- initialize: returns protocol version and server info.
- tools/list: returns tools filtered by the caller's scope.
- tools/call: calls a tool by name, with arguments,
  and returns content text, or an error if the tool
  is not in the caller's scope, or does not exist.
- ping: returns an empty result.

Tools, by scope:
- owner: submit_goal, read_goal, list_goals, steer_goal,
  cancel_goal, approve_goal, list_keys.
- agent: submit_goal, read_goal, list_goals, steer_goal,
  cancel_goal, only for goals that agent submitted.
  No approve_goal, no list_keys, in tools/list or
  in tools/call, for an agent token.

Transport: stdio JSON-RPC, one JSON object per line,
in run_stdio(), below. Streamable HTTP MCP is future,
not built here, because the v1 HTTP API already
serves HTTP clients, and stdio MCP serves local
model clients, which is the hackathon use case.
A real key is never returned by an MCP tool, only
by v1 key generation, once, and never in a tool list.
"""

from __future__ import annotations

import json
import sys

from .v1 import AuthStore, GoalStore


def _tools_for_scope(scope: str) -> list[dict]:
    tools = [
        {"name": "submit_goal", "description": "Submit a goal, returns a goal id."},
        {"name": "read_goal", "description": "Read a goal by id."},
        {"name": "list_goals", "description": "List goals visible to this token."},
        {"name": "steer_goal", "description": "Add a steer note to a goal."},
        {"name": "cancel_goal", "description": "Cancel a goal."},
    ]
    if scope == "owner":
        tools.append({"name": "approve_goal", "description": "Approve a goal, owner only."})
        tools.append({"name": "list_keys", "description": "List key hash prefixes, owner only."})
    return tools


class MCPServer:
    def __init__(self, auth: AuthStore, goals: GoalStore):
        self.auth = auth
        self.goals = goals

    def handle(self, request: dict, token: str | None) -> dict:
        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params", {}) or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"protocolVersion": "2024-11-05",
                               "serverInfo": {"name": "aidon", "version": "v1"}}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        record = self.auth.verify(token)
        if record is None:
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32001, "message": "missing or invalid token"}}
        scopes = self.auth.scopes_for(record)
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id,
                    "result": {"tools": _tools_for_scope(record.scope)}}
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            allowed = {t["name"] for t in _tools_for_scope(record.scope)}
            if name not in allowed:
                return {"jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32002, "message": "tool not in scope"}}
            if name == "submit_goal":
                if "submit" not in scopes:
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32002, "message": "insufficient scope"}}
                goal = self.goals.submit(str(args.get("goal", "")),
                                         str(args.get("project_id", "default")),
                                         owner_name=record.name)
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(goal.to_dict())}]}}
            if name == "read_goal":
                goal = self.goals.get(str(args.get("id", "")))
                if goal is None or (record.scope == "agent" and goal.owner_name != record.name):
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32003, "message": "goal not found"}}
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(goal.to_dict())}]}}
            if name == "list_goals":
                visible = self.goals.visible_to(record)
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps([g.to_dict() for g in visible])}]}}
            if name == "steer_goal":
                goal = self.goals.get(str(args.get("id", "")))
                if goal is None or (record.scope == "agent" and goal.owner_name != record.name):
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32003, "message": "goal not found"}}
                goal.steer_notes.append(str(args.get("note", "")))
                self.goals._save()
                try:
                    from .run_control import RunControl
                    control = RunControl(self.goals.home)
                    if control.get(str(args.get("id", ""))) is None:
                        control.register(str(args.get("id", "")), goal.goal, goal.project_id)
                    control.steer(str(args.get("id", "")), str(args.get("note", "")))
                except Exception:
                    pass
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(goal.to_dict())}]}}
            if name == "cancel_goal":
                goal = self.goals.get(str(args.get("id", "")))
                if goal is None or (record.scope == "agent" and goal.owner_name != record.name):
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32003, "message": "goal not found"}}
                goal.status = "cancelled"
                self.goals._save()
                try:
                    from .run_control import RunControl
                    RunControl(self.goals.home).cancel(str(args.get("id", "")))
                except Exception:
                    pass
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(goal.to_dict())}]}}
            if name == "approve_goal":
                goal = self.goals.get(str(args.get("id", "")))
                if goal is None:
                    return {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32003, "message": "goal not found"}}
                goal.status = "approved"
                self.goals._save()
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(goal.to_dict())}]}}
            if name == "list_keys":
                return {"jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": json.dumps(self.auth.list_keys())}]}}
            return {"jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32601, "message": "unknown tool"}}
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "unknown method"}}

    def run_stdio(self, token: str | None = None) -> None:
        """Stdio JSON-RPC loop, one JSON object per line, token from AIDON_MCP_TOKEN."""
        import os
        token = token or os.environ.get("AIDON_MCP_TOKEN", "")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({"jsonrpc": "2.0", "id": None,
                                  "error": {"code": -32700, "message": "parse error"}}), flush=True)
                continue
            response = self.handle(request, token)
            print(json.dumps(response), flush=True)
