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
import threading
import queue
import select
import time

# Re-exported here so callers of the stdio client use the same
# expansion as the HTTP client: "$VAR" / "${VAR}" / "Bearer $VAR"
# expand from the environment at connect time, and the variable
# name is what an approval card should show.
from .mcp_http import expand_env_value

__all__ = ["McpClient", "expand_env_value"]


class McpClient:
    def __init__(self, command: list[str], env: dict | None = None, timeout: float = 30):
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
        self.timeout = max(0.01, float(timeout))
        self._lock = threading.Lock()
        self._responses = queue.Queue(maxsize=128)

    def start(self) -> None:
        # Pass only runtime necessities and explicitly configured bindings.
        # A third-party MCP server must not inherit Aidon's API keys.
        inherited = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
                     "WINDIR", "USERPROFILE", "SSL_CERT_FILE", "SSL_CERT_DIR",
                     "REQUESTS_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                     "http_proxy", "https_proxy", "no_proxy")
        merged_env = {key: os.environ[key] for key in inherited if key in os.environ}
        merged_env.update(self.env)
        self.proc = subprocess.Popen(self.command, stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, text=True,
                                     env=merged_env)
        proc = self.proc
        def read_responses():
            try:
                while True:
                    line = proc.stdout.readline(1048577)
                    if not line or len(line) > 1048576 or not line.endswith("\n"):
                        break
                    self._responses.put_nowait(line)
            except (OSError, ValueError, queue.Full):
                pass
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass
        threading.Thread(target=read_responses, daemon=True).start()
        try:
            self._call("initialize", {"protocolVersion": "2024-11-05",
                                      "capabilities": {}, "clientInfo": {"name": "aidon", "version": "1"}})
        except Exception:
            self.close()
            raise

    def _call(self, method: str, params: dict):
        with self._lock:
            self._counter += 1
            request = {"jsonrpc": "2.0", "id": self._counter, "method": method, "params": params}
            deadline = time.monotonic() + self.timeout
            encoded = json.dumps(request) + "\n"
            if len(encoded.encode("utf-8")) > 1048576:
                raise ValueError("MCP request exceeds size limit")
            proc = self.proc
            if proc is None or proc.stdin is None:
                raise ValueError("MCP server is not running")
            # A worker thread around buffered TextIO.write only bounds the
            # caller: it can remain blocked forever while holding a reference
            # to the pipe. Use nonblocking writes on the pipe descriptor so
            # backpressure is governed by the same request deadline.
            payload = encoded.encode("utf-8")
            fd = proc.stdin.fileno()
            try:
                os.set_blocking(fd, False)
                view = memoryview(payload)
                while view:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    try:
                        written = os.write(fd, view)
                    except BlockingIOError:
                        _, writable, _ = select.select([], [fd], [], remaining)
                        if not writable:
                            raise TimeoutError
                        continue
                    if written <= 0:
                        raise BrokenPipeError("MCP server closed its input")
                    view = view[written:]
            except TimeoutError:
                self.close()
                raise TimeoutError(f"MCP {method} write timed out") from None
            except (OSError, ValueError) as exc:
                self.close()
                raise ValueError("MCP server closed its input") from exc
            while True:
                try:
                    line = self._responses.get(timeout=max(0, deadline - time.monotonic()))
                except queue.Empty:
                    self.close()
                    raise TimeoutError(f"MCP {method} timed out") from None
                if line is None:
                    raise ValueError("MCP server closed the connection")
                data = json.loads(line)
                if not isinstance(data, dict) or data.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid MCP response")
                if "id" not in data:  # notification
                    continue
                if data["id"] != request["id"]:
                    raise ValueError("MCP response ID mismatch")
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
                self.proc.wait(timeout=5)
            self.proc = None
