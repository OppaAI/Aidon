"""Register per-run, approval-gated MCP client tools."""

from __future__ import annotations

import json


def register_mcp_tools(config, tools: dict, approval, governor=None) -> None:
    """Install connect/disconnect tools and namespace acquired remote tools."""
    # MCP client tools from Aidon. Connections live for this goal run;
    # every new stdio connection is approval-gated before third-party code
    # is started. Server tools are exposed with a namespaced name so they
    # cannot collide with Aidon's own tools.
    mcp_connections = {}

    def mcp_connect(name, transport="stdio", command="", args=None,
                    env=None, url="", token="", test_tool="",
                    test_args=None):
        import shlex
        server_name = str(name).strip()
        if not server_name:
            return "MCP_REFUSED: server name is required"
        if server_name in mcp_connections:
            return f"MCP_ALREADY_CONNECTED {server_name!r}"
        transport = str(transport or "stdio").lower()
        if transport == "stdio":
            command_parts = shlex.split(str(command)) + [str(x) for x in (args or [])]
            if not command_parts:
                return "MCP_REFUSED: command is required for stdio transport"
            mcp_config = {"transport": "stdio",
                          "command": str(command),
                          "args": [str(x) for x in (args or [])],
                          "env": dict(env or {})}

            def _factory(_cfg, _parts=command_parts, _env=dict(env or {})):
                from ..interface.mcp_client import McpClient
                _client = McpClient(_parts, env=_env)
                _client.start()
                return _client
        elif transport in ("http", "streamable-http"):
            if not url:
                return "MCP_REFUSED: url is required for HTTP transport"
            mcp_config = {"transport": transport, "url": str(url),
                          "token": str(token or "")}

            def _factory(_cfg, _url=str(url), _token=str(token or "")):
                from ..interface.mcp_http import McpHttpClient
                _client = McpHttpClient(_url, token=_token or None)
                _client.initialize()
                return _client
        else:
            return f"MCP_REFUSED: unsupported transport {transport!r}"
        # One approval, through the acquisition lifecycle: the
        # lifecycle approves (bound to the exact command/URL),
        # connects, lists, scans descriptions, and registers
        # the server as validated; this tool then keeps the
        # client for the run's namespaced tools. The governor
        # counts once, inside the lifecycle after approval.
        _why = {}

        class _Decision:
            def __init__(self, verdict):
                self.verdict = verdict

        def _acquire_approve(kind, title, body):
            ok, why = approval(kind, title, body, force_ask=True)
            if not ok:
                _why["why"] = why
            return _Decision("approved" if ok else "rejected")

        from ..capabilities.acquisition_governor import AcquisitionBudgetExceeded
        from ..capabilities.mcp_acquisition import acquire_mcp_server
        try:
            if test_args is not None and not str(test_tool or "").strip():
                return "MCP_REFUSED: test_args requires test_tool"
            if test_args is not None and not isinstance(test_args, dict):
                return "MCP_REFUSED: test_args must be an object"
            test_call = ({"tool": str(test_tool),
                          "args": dict(test_args or {})}
                         if str(test_tool or "").strip() else None)
            result = acquire_mcp_server(
                config.home, server_name, mcp_config, _acquire_approve,
                client_factory=_factory, governor=governor, keep_client=True,
                test_call=test_call)
        except AcquisitionBudgetExceeded:
            raise
        except Exception as exc:
            if "rejected by approval" in str(exc) and _why.get("why"):
                return f"MCP_REFUSED: {_why['why']}"
            return f"MCP_REFUSED: {type(exc).__name__}: {exc}"
        client = result["client"]
        mcp_connections[server_name] = client
        registered = []
        for remote_name in result["tools"]:
            local_name = f"mcp_{server_name}_{remote_name}"

            def call_remote(_args=None, _client=client, _remote=remote_name):
                return json.dumps(_client.call_tool(_remote, _args or {}), ensure_ascii=False)

            tools[local_name] = call_remote
            registered.append(local_name)
        try:
            from ..capabilities.capability_registry import CapabilityRegistry
            CapabilityRegistry(config.home).record_use(server_name)
        except Exception:
            pass
        scan_note = ""
        if result.get("scan_findings"):
            scan_note = (f"; description scan findings: "
                         f"{len(result['scan_findings'])} (informational, not blocking)")
        return f"MCP_CONNECTED {server_name!r}: {', '.join(registered) or 'no safe tools'}{scan_note}"

    def mcp_disconnect(name):
        server_name = str(name).strip()
        client = mcp_connections.pop(server_name, None)
        if client is None:
            return f"MCP: no open connection named {server_name!r}"
        client.close()
        prefix = f"mcp_{server_name}_"
        for tool_name in list(tools):
            if tool_name.startswith(prefix):
                tools.pop(tool_name, None)
        return f"MCP_DISCONNECTED {server_name!r}"

    tools["mcp_connect"] = mcp_connect
    tools["mcp_disconnect"] = mcp_disconnect

