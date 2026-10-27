"""MCP over HTTP, the transport the stdio client does not cover.

A hosted MCP server, such as a Gmail or Workspace MCP server, is
reached over HTTP, not by spawning a local process. This client
speaks JSON-RPC over one POST endpoint: initialize, then
tools/list and tools/call, protocol version 2024-11-05. A Bearer
token is sent as a header when one is given, never as a tool
argument: an argument value that contains the token is refused
before any request is made, so a secret cannot leak into a tool
call log on the server side.

Two response shapes are accepted, because Streamable HTTP
servers vary: a plain JSON object, and an SSE stream, in which
case the last ``data:`` line carrying JSON is the result. An
``Mcp-Session-Id`` response header, when present, is sent back
on later calls, as Streamable HTTP servers expect.

No network call happens at import time. Tests use a local HTTP
server double, never a real endpoint.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"


class McpHttpError(Exception):
    """A JSON-RPC error, or an HTTP failure, from the MCP server."""


def _parse_response(body: bytes, content_type: str) -> dict:
    text = body.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in (content_type or ""):
        payload = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                candidate = line[len("data:"):].strip()
                if candidate and candidate != "[DONE]":
                    payload = candidate
        if payload is None:
            raise McpHttpError("SSE response carried no data line")
        text = payload
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpHttpError(f"server returned non-JSON: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise McpHttpError("server returned a non-object JSON-RPC response")
    return data


class McpHttpClient:
    """One MCP server over HTTP, JSON-RPC, Bearer token optional."""

    def __init__(self, endpoint: str, token: str | None = None,
                 timeout: float = 30.0):
        if not endpoint:
            raise ValueError("MCP HTTP endpoint is required")
        self.endpoint = endpoint.rstrip("/")
        self.token = token or None
        self.timeout = timeout
        self.session_id: str | None = None
        self.server_info: dict = {}
        self._counter = 0
        self._closed = False

    def _check_arguments(self, arguments: dict) -> None:
        if not self.token:
            return
        for key, value in (arguments or {}).items():
            if isinstance(value, str) and self.token and self.token in value:
                raise ValueError(
                    f"tool argument {key!r} contains the auth token; "
                    "secrets are headers, never tool arguments")

    def _post(self, method: str, params: dict) -> dict:
        if self._closed:
            raise McpHttpError("MCP HTTP client is closed")
        self._counter += 1
        payload = {"jsonrpc": "2.0", "id": self._counter,
                   "method": method, "params": params}
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(),
            method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                session = resp.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                data = _parse_response(resp.read(),
                                       resp.headers.get("Content-Type", ""))
        except urllib.error.HTTPError as exc:
            raise McpHttpError(f"HTTP {exc.code} from MCP server") from exc
        except urllib.error.URLError as exc:
            raise McpHttpError(f"MCP server unreachable: {exc}") from exc
        if "error" in data:
            err = data["error"]
            message = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise McpHttpError(f"MCP error: {message}")
        return data.get("result", {})

    def initialize(self) -> dict:
        """Handshake. Returns the server's result, stored on the client."""
        result = self._post("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aidon", "version": "1"},
        })
        self.server_info = result.get("serverInfo", {}) if isinstance(result, dict) else {}
        return result

    def list_tools(self) -> list[dict]:
        """Tools the server offers, as plain dicts."""
        result = self._post("tools/list", {})
        tools = result.get("tools", []) if isinstance(result, dict) else []
        return list(tools)

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        """Call one tool. Returns the raw result dict, unfiltered."""
        arguments = arguments or {}
        self._check_arguments(arguments)
        return self._post("tools/call", {"name": name, "arguments": arguments})

    def close(self) -> None:
        """Mark closed. HTTP is stateless, so there is no session to tear down."""
        self._closed = True
