"""MCP acquisition and health checks: the last hub item.

Discovery today is manual: the planner searches the web
and proposes a config. The per-use mcp_connect tool runs
the acquisition lifecycle below: one force_ask approval
bound to the exact command or URL, then connect, list,
scan, and register as validated, keeping the client for
the run's namespaced tools. Acquisition turns that path
into a tested, approval-based lifecycle:

  propose -> approve (exact command/URL) -> connect
  -> list tools -> scan descriptions -> register
  (validated) -> promote (trusted, separate approval)

and health checks keep the record honest afterwards:

  connect -> list tools -> compare with registered
  tools -> record healthy / degraded / unavailable.

Approval, twice, deliberately. Gate 1 (kind "install",
the same kind the per-use connect tool uses) approves
starting the server's code or contacting its URL, bound
to the exact command or URL in the card body. Gate 2
is the registry's promote approval: registration alone
is validated, never usable, so a registered server
cannot run until a human trusts it. The approve
callback shape matches the registry's: approve(kind,
title, body) returning an object with .verdict in
("approved", "auto_approved").

What acquisition does NOT do:

- It does not call tools speculatively. A tool call
  with guessed arguments risks side effects (send,
  publish, delete) and false failures (a tool that
  needs arguments fails without them, which says
  nothing about the server). The non-destructive test
  is connect + list_tools, which exercises the full
  JSON-RPC round trip. An explicit test_call
  ({tool, args}, supplied by the proposer) is executed
  when given, and only then.
- It does not persist secrets. The stored registry
  config keeps transport, command, args, url, and the
  discovered tools — never env values or tokens. A
  server that needs a token takes it at each connect
  from the environment ($VAR form), exactly as the
  per-use tool does.
- It does not keep the connection, unless the caller
  passes keep_client: the per-use tool keeps it for the
  run and closes it on disconnect. Otherwise acquisition
  registers; later runs connect per-use, as today.
- It does not block on scan findings. Tool names are
  validated (alphanumeric plus underscore, the same
  rule the per-use tool uses to namespace them), and
  descriptions are scanned with the injection
  heuristics; findings are reported in the result for
  the promoter to weigh, not silently passed and not
  over-blocked on heuristic hits.

Health semantics: healthy means connect + list
succeeded and the live tools match the registered
tools exactly. Degraded means the server answers but
its tools drifted (missing or new names) — the
registered record no longer describes it. Unavailable
means connect or list failed. A retired server is
never probed. Health checks reconnect only to
configs already present in the registry or the
operator file: they are not new grants, and they
take no approval, because the server was approved
at acquisition (or configured by the operator) and
the check uses that same command or URL.

The client factory contract: factory(config) returns
a CONNECTED client with list_tools(), call_tool(name,
args), and close(). The default factory builds the
stdio or HTTP client from the config and connects it.
Tests inject a fake.
"""

from __future__ import annotations

import json
import shlex
import hashlib
import copy
import re
from pathlib import Path

SAFE_TOOL_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def _safe_tool_name(name: str) -> bool:
    name = (name or "").strip()
    return bool(name) and all(c in SAFE_TOOL_CHARS
                              for c in name.lower())


def _check_approval(decision) -> None:
    verdict = getattr(decision, "verdict", None)
    if verdict not in ("approved", "auto_approved"):
        raise RuntimeError("MCP acquisition rejected by approval")


def _scan_descriptions(tools: list) -> list:
    """Injection-heuristic findings over tool descriptions.
    Informational only: returned for the promoter, never
    silently passed, never a block on heuristic hits."""
    try:
        from .security_gate import injection_heuristics
    except Exception:
        return []
    findings = []
    for tool in tools:
        text = f"{tool.get('name', '')}: {tool.get('description', '')}"
        for finding in injection_heuristics(text):
            findings.append({"tool": tool.get("name", ""),
                             "type": getattr(finding, "type", "injection"),
                             "message": getattr(finding, "message", "")})
    return findings


def _default_client(config: dict):
    transport = str(config.get("transport", "stdio")).lower()
    if transport == "stdio":
        from ..interface.mcp_client import McpClient
        parts = shlex.split(str(config.get("command", ""))) + \
            [str(x) for x in (config.get("args") or [])]
        if not parts:
            raise ValueError("MCP stdio config needs a command")
        client = McpClient(parts, env=dict(config.get("env") or {}))
        client.start()
        return client
    if transport in ("http", "streamable-http"):
        from ..interface.mcp_http import McpHttpClient
        url = str(config.get("url", ""))
        if not url:
            raise ValueError("MCP http config needs a url")
        client = McpHttpClient(url, token=config.get("token") or None)
        client.initialize()
        return client
    raise ValueError(f"unknown MCP transport: {transport!r}")


def _approval_details(config: dict, test_call: dict | None) -> str:
    """Describe exactly what will connect/run, without exposing secrets."""
    from ..interface.mcp_http import expand_env_value

    transport = str(config.get("transport", "stdio")).lower()
    if transport == "stdio":
        argv = [str(config.get("command", ""))] + [
            str(x) for x in config.get("args") or []]
        safe_argv = []
        redact_next = False
        for value in argv:
            if redact_next:
                safe_argv.append("[redacted]")
                redact_next = False
                continue
            match = re.match(
                r"^(--?(?:.*(?:password|secret|token|api[_-]?key|authorization)))(?:=(.*))?$",
                value, re.IGNORECASE)
            if match:
                safe_argv.append(match.group(1) +
                                 ("=[redacted]" if match.group(2) is not None else ""))
                redact_next = match.group(2) is None
                continue
            try:
                from ..memory.redaction import redact_text
                value = redact_text(value)
            except Exception:
                pass
            safe_argv.append(value)
        target = json.dumps(
            safe_argv, ensure_ascii=False)
    else:
        from urllib.parse import (parse_qsl, urlencode, urlsplit,
                                  urlunsplit)
        raw_url = str(config.get("url", ""))
        try:
            parsed = urlsplit(raw_url)
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            if parsed.username is not None or parsed.password is not None:
                host = "[redacted]@" + host
            query = urlencode([
                (key, "[redacted]" if re.search(
                    r"password|secret|token|signature|credential|auth|api[_-]?key",
                    key, re.IGNORECASE) else value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)])
            target = urlunsplit((parsed.scheme, host, parsed.path, query, ""))
        except ValueError:
            target = "[invalid URL; exact value bound by fingerprint]"
    env_bindings = []
    for key, value in sorted(dict(config.get("env") or {}).items()):
        _, variable = expand_env_value(value)
        env_bindings.append(
            f"{key} <- ${variable}" if variable else
            f"{key} <- [literal value redacted]")
    token = config.get("token")
    if token:
        _, variable = expand_env_value(token)
        env_bindings.append(
            f"Authorization <- ${variable}" if variable else
            "Authorization <- [literal value redacted]")
    bound = {"config": config, "test_call": test_call}
    fingerprint = hashlib.sha256(json.dumps(
        bound, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()
    details = [f"Transport: {transport}", f"Target: {target}",
               f"Config fingerprint (SHA-256): {fingerprint}"]
    details.append("Environment bindings: " +
                   (", ".join(env_bindings) if env_bindings else "none"))
    if test_call is None:
        details.append(
            "Functional test: none; this approval only permits connect/list-tools.")
    else:
        details.append(
            "Functional test: call exactly one tool once after listing it: "
            + str(test_call["tool"]))
        # The owner must see the action inputs before approving it. Secret-like
        # fields are redacted; the full request is covered by the fingerprint.
        def redact(value, key=""):
            import re
            if re.search(r"password|secret|token|api[_-]?key|authorization",
                         str(key), re.IGNORECASE):
                return "[redacted]"
            if isinstance(value, dict):
                return {str(k): redact(v, k) for k, v in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value
        details.append("Test arguments: " + json.dumps(
            redact(test_call.get("args") or {}), ensure_ascii=False,
            sort_keys=True, default=str)[:2000])
    return "\n".join(details)


def _stored_config(config: dict, tools: list) -> dict:
    """Registry config: everything needed to reconnect,
    minus secrets. Env values and tokens are never stored."""
    stored = {"transport": str(config.get("transport", "stdio")).lower(),
              "tools": list(tools)}
    if stored["transport"] == "stdio":
        stored["command"] = str(config.get("command", ""))
        stored["args"] = [str(x) for x in (config.get("args") or [])]
    else:
        stored["url"] = str(config.get("url", ""))
    return stored


def acquire_mcp_server(home, name: str, config: dict, approve,
                       client_factory=None, test_call: dict | None = None,
                       governor=None, keep_client: bool = False) -> dict:
    """Approve, connect, list, scan, and register an MCP
    server as validated. Trust is separate: promote the
    returned entry with the registry's promote approval.

    When a governor is given, the connect/list/register
    phase runs inside governor.timed("mcp") AFTER approval
    passes, so a rejected approval consumes no budget and
    a failed attempt still does — the same rule as the
    per-use mcp_connect tool.

    Re-acquiring an already registered server with the
    same connection config (transport, command/args, or
    URL) succeeds without duplicating the record and
    returns the existing entry; a different config raises
    until the old entry is retired. With keep_client the
    connected client is returned in the result instead
    of closed, for the caller to use and close."""
    from .capability_registry import CapabilityRegistry
    # Freeze the exact target and bindings shown in the approval card. Callers
    # must not be able to change the approved config before the process starts.
    config = copy.deepcopy(dict(config))
    name = str(name).strip()
    if not name:
        raise ValueError("MCP server name is required")
    transport = str(config.get("transport", "stdio")).lower()
    if transport == "stdio" and not str(config.get("command", "")).strip():
        raise ValueError("MCP stdio config needs a command")
    if transport in ("http", "streamable-http") and not config.get("url"):
        raise ValueError("MCP http config needs a url")
    if transport not in ("stdio", "http", "streamable-http"):
        raise ValueError(f"unknown MCP transport: {transport!r}")
    if test_call is not None:
        if not isinstance(test_call, dict) or not str(test_call.get("tool", "")):
            raise ValueError("MCP functional test needs a tool name")
        if not isinstance(test_call.get("args", {}), dict):
            raise ValueError("MCP functional test args must be an object")
        test_call = {"tool": str(test_call["tool"]),
                     "args": copy.deepcopy(test_call.get("args") or {})}
    if approve is None:
        raise RuntimeError("MCP acquisition needs human approval")
    _check_approval(approve(
        "install", f"Connect MCP server {name}?",
        f"Start/connect to MCP server {name!r}:\n"
        f"{_approval_details(config, test_call)}"))
    from contextlib import nullcontext
    _gov_ctx = governor.timed("mcp") if governor is not None else nullcontext()
    with _gov_ctx:
        factory = client_factory or _default_client
        client = factory(config)
        result = None
        try:
            offered = client.list_tools() or []
            safe = [str(t.get("name", "")).strip() for t in offered]
            safe = [n for n in safe if _safe_tool_name(n)]
            safe = list(dict.fromkeys(safe))
            if not safe:
                raise RuntimeError(
                    f"MCP server {name!r} offered no safe tools")
            evidence = [
                {"test": "connect", "passed": True, "transport": transport},
                {"test": "list_tools", "passed": True, "tools": safe},
            ]
            if test_call is not None:
                tool = str(test_call.get("tool", ""))
                if tool not in safe:
                    raise ValueError(
                        f"test tool {tool!r} is not an offered tool")
                result = client.call_tool(tool, dict(test_call.get("args") or {}))
                if isinstance(result, dict) and result.get("isError") is True:
                    raise RuntimeError(
                        f"MCP functional test failed for {tool!r}")
                result_bytes = json.dumps(
                    result, ensure_ascii=False, sort_keys=True,
                    default=str).encode("utf-8")
                evidence.append({"test": "call_tool", "passed": True,
                                 "tool": tool,
                                 "output_type": type(result).__name__,
                                 "output_sha256": hashlib.sha256(
                                     result_bytes).hexdigest()})
            findings = _scan_descriptions(offered)
            stored = _stored_config(config, safe)
            registry = CapabilityRegistry(home)
            existing = registry.get(name)
            if existing is not None and existing.get("status") != "retired":
                econ = existing.get("config") or {}
                same = (econ.get("transport") == stored.get("transport")
                        and econ.get("command", "") == stored.get("command", "")
                        and list(econ.get("args") or []) == list(stored.get("args") or [])
                        and econ.get("url", "") == stored.get("url", ""))
                if not same:
                    raise ValueError(
                        f"capability already registered: {name!r} with different "
                        "config; retire it first")
                result = {"entry": existing, "tools": safe, "evidence": evidence,
                          "scan_findings": findings, "already_registered": True}
            else:
                entry = registry.register(name, "mcp", stored, evidence)
                result = {"entry": entry, "tools": safe, "evidence": evidence,
                          "scan_findings": findings, "already_registered": False}
        finally:
            if not (keep_client and result is not None):
                try:
                    client.close()
                except Exception:
                    pass
        if keep_client and result is not None:
            result["client"] = client
        return result
def _resolve_mcp_config(home, name: str):
    """Registered config first, operator config second.
    Returns (config, registered_entry_or_None)."""
    from .capability_registry import CapabilityRegistry
    entry = CapabilityRegistry(home).get(name)
    if entry is not None and entry.get("kind") == "mcp":
        return entry.get("config") or {}, entry
    try:
        data = json.loads((Path(home) / "mcp_servers.json").read_text())
    except Exception:
        data = None
    if isinstance(data, dict):
        config = data.get(name)
        if isinstance(config, dict):
            return config, None
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and str(item.get("name", "")) == name:
                return item, None
    raise ValueError(f"unknown MCP server: {name}")


def check_mcp_health(home, name: str, client_factory=None) -> dict:
    """Probe one MCP server and record its health in the hub.
    Retired servers are never probed. Connection failures
    record unavailable; tool drift records degraded."""
    from .capability_hub import CapabilityHub
    config, entry = _resolve_mcp_config(home, name)
    if entry is not None and entry.get("status") == "retired":
        return {"name": name, "health": "unknown",
                "note": "retired; not probed"}
    hub = CapabilityHub(home)
    transport = str(config.get("transport", "stdio")).lower()
    availability = "remote" if transport in ("http", "streamable-http") \
        else "local"
    factory = client_factory or _default_client
    try:
        client = factory(config)
        try:
            live = [str(t.get("name", "")).strip()
                    for t in (client.list_tools() or [])]
            live = [n for n in live if _safe_tool_name(n)]
            live = list(dict.fromkeys(live))
        finally:
            try:
                client.close()
            except Exception:
                pass
    except Exception as exc:
        note = f"connect failed: {type(exc).__name__}: {exc}"[:200]
        hub.record_health(f"mcp:{name}", "unavailable",
                          availability=availability, note=note)
        return {"name": name, "health": "unavailable", "note": note}
    registered = []
    if entry is not None:
        registered = list((entry.get("config") or {}).get("tools") or [])
    if registered and set(live) != set(registered):
        missing = sorted(set(registered) - set(live))
        new = sorted(set(live) - set(registered))
        note = f"tools drifted: missing {missing}; new {new}"[:200]
        hub.record_health(f"mcp:{name}", "degraded",
                          availability=availability, note=note)
        return {"name": name, "health": "degraded", "note": note,
                "tools_live": live}
    hub.record_health(f"mcp:{name}", "healthy",
                      availability=availability)
    return {"name": name, "health": "healthy", "tools_live": live}


def check_all_mcp_health(home, client_factory=None) -> list:
    """Probe every hub MCP entry except retired ones.
    A per-server failure records unavailable (fail-closed)
    rather than aborting the sweep."""
    from .capability_hub import CapabilityHub
    results = []
    for entry in CapabilityHub(home).list(kind="mcp"):
        if entry["lifecycle"] == "retired":
            continue
        try:
            results.append(check_mcp_health(home, entry["name"],
                                            client_factory))
        except Exception as exc:
            note = f"health check failed: {type(exc).__name__}"[:200]
            try:
                CapabilityHub(home).record_health(
                    f"mcp:{entry['name']}", "unavailable", note=note)
            except Exception:
                pass
            results.append({"name": entry["name"], "health": "unavailable",
                            "note": note})
    return results
