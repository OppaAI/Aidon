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

A token may be given as "$VAR" or "Bearer $VAR": it is expanded
from the environment at connect time (see expand_env_value),
and the client records which variable supplied it as
``token_var``. The expanded value, never the "$VAR" text, is
what goes into the Authorization header.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2024-11-05"

# A $VAR reference: "$NAME" or "${NAME}", NAME = [A-Z0-9_]+.
_VAR_REF_RE = re.compile(r"\$(?:\{([A-Z0-9_]+)\}|([A-Z0-9_]+))")


def expand_env_value(value) -> tuple[str, str | None]:
    """Expand $VAR references in a token/header value.

    Returns (expanded, variable_name). The variable name is the
    first $NAME reference found, or None when the value contains
    no reference at all — that name is what an approval card
    should show, so the owner sees *which* variable supplies the
    secret instead of the secret itself. A reference to an unset
    variable expands to "" (documented, not an error): the
    caller then has no token, rather than a literal "$NAME"
    sent to a server. A value with no $ reference is returned
    unchanged with None.
    """
    text = "" if value is None else str(value)
    names: list[str] = []

    def _replace(match: "re.Match[str]") -> str:
        name = match.group(1) or match.group(2)
        names.append(name)
        return os.environ.get(name, "")

    expanded = _VAR_REF_RE.sub(_replace, text)
    return expanded, (names[0] if names else None)


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
        if token is None:
            self.token = None
            self.token_var = None
        else:
            # $VAR form is the recommended form: the token is
            # expanded at connect time from the environment, and
            # token_var records which variable supplied it, for
            # display on an approval card. A literal token is
            # still accepted as-is (not refused here); only the
            # $VAR form gets a token_var.
            expanded, var = expand_env_value(token)
            # The header adds "Bearer " itself, so a caller using
            # the old "Bearer $VAR" form must not end up with a
            # doubled "Bearer Bearer" header: strip a leading
            # scheme from the expanded value.
            if expanded.lower().startswith("bearer "):
                expanded = expanded[len("bearer "):]
            self.token = expanded or None
            self.token_var = var
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
