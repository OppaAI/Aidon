"""MCP client: use an external MCP server, stdio JSON-RPC, stdlib only.

New Aidon already serves MCP (src/mcp_server.py). This is the
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
import subprocess


class McpClient:
    def __init__(self, command: list[str]):
        if not command:
            raise ValueError("MCP server command is required")
        self.command = command
        self.proc = None
        self._counter = 0

    def start(self) -> None:
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True)
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
