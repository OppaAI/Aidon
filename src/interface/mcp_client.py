"""MCP client: use an external MCP server, stdio JSON-RPC, stdlib only.

The repository already serves MCP (src/interface/mcp_server.py). This is the
other direction: Aidon as a client, calling tools on a server
someone else runs, for example a Gmail Workspace MCP server.
The client spawns the server command, sends initialize with
protocol 2024-11-05, lists tools, calls a tool, and closes
the server. It never passes a secret in a tool argument that
is not the server's own auth, and it never trusts a tool
result as a plan: results go through the same verification
as any other tool result.
"""

from __future__ import annotations

import json
import os
import subprocess

# Re-exported here so callers of the stdio client use the same
# expansion as the HTTP client: "$VAR" / "${VAR}" / "Bearer $VAR"
# expand from the environment at connect time, and the variable
# name is what an approval card should show.
from .mcp_http import expand_env_value

__all__ = ["McpClient", "expand_env_value"]


class McpClient:
    def __init__(self, command: list[str], env: dict | None = None):
        if not command:
            raise ValueError("MCP server command is required")
        self.command = command
        # Token/header values for an MCP connection live in env:
        # each value is expanded through expand_env_value, so a
        # "$VAR" entry becomes the environment's value at start
        # time and never travels as literal "$VAR" text. env_vars
        # maps each env key to the variable name that supplied
        # it (or None for a literal value); token_var is the
        # first variable referenced, for approval-card display,
        # or None when no env entry references a variable. A
        # literal secret value is NOT refused here — a caller
        # may pass a real token directly — the $VAR form is the
        # recommended form, and the variable name is exposed
        # precisely so a card can show it instead of the secret.
        self.env: dict[str, str] = {}
        self.env_vars: dict[str, str | None] = {}
        for key, value in dict(env or {}).items():
            expanded, var = expand_env_value(value)
            self.env[str(key)] = expanded
            self.env_vars[str(key)] = var
        self.token_var = next(
            (var for var in self.env_vars.values() if var is not None), None)
        self.proc = None
        self._counter = 0

    def start(self) -> None:
        merged_env = dict(os.environ)
        merged_env.update(self.env)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True,
                                     env=merged_env)
        self._call("initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {"name": "aidon", "version": "1"}})

    def _call(self, method: str, params: dict):
        self._counter += 1
        request = {"jsonrpc": "2.0", "id": self._counter, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise ValueError("MCP server closed the connection")
        data = json.loads(line)
        if "error" in data:
            raise ValueError(f"MCP error: {data['error']}")
        return data.get("result", {})

    def list_tools(self) -> list[dict]:
        return self._call("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict):
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        if self.proc is not None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
            self.proc = None
