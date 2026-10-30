"""MCP acquisition: approval gates, tool discovery, secret
stripping, registration as validated, and health checks."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.capability_hub import CapabilityHub
from src.capabilities.capability_registry import CapabilityRegistry
from src.capabilities.mcp_acquisition import (
    acquire_mcp_server, check_all_mcp_health, check_mcp_health)


def _approve(kind, title, body):
    assert kind == "install"
    return SimpleNamespace(verdict="approved")


def _reject(kind, title, body):
    return SimpleNamespace(verdict="rejected")


class _FakeClient:
    def __init__(self, tools=(), fail=False):
        self.tools = [{"name": n, "description": d}
                      for n, d in tools]
        self.fail = fail
        self.calls = []
        self.closed = False

    def list_tools(self):
        if self.fail:
            raise ConnectionError("server down")
        return self.tools

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"ok": True}

    def close(self):
        self.closed = True


def _factory_for(fake, seen=None):
    def factory(config):
        if seen is not None:
            seen.append(config)
        if fake.fail:
            raise ConnectionError("server down")
        return fake
    return factory


def test_acquire_stdio_registers_validated(tmp_path):
    fake = _FakeClient([("search_papers", "Search papers."),
                        ("fetch_page", "Fetch a page.")])
    result = acquire_mcp_server(
        tmp_path, "papers",
        {"transport": "stdio", "command": "papers-mcp",
         "args": ["--fast"], "env": {"TOKEN": "$PAPERS_TOKEN"}},
        _approve, client_factory=_factory_for(fake))
    entry = result["entry"]
    assert entry["status"] == "validated"
    assert entry["config"]["tools"] == ["search_papers", "fetch_page"]
    assert entry["config"]["command"] == "papers-mcp"
    assert "env" not in entry["config"]
    assert "token" not in entry["config"]
    assert result["tools"] == ["search_papers", "fetch_page"]
    assert [e["test"] for e in result["evidence"]] == [
        "connect", "list_tools"]
    assert fake.closed is True
    assert CapabilityRegistry(tmp_path).usable() == [], \
        "validated is not usable; trust needs promotion"
    hub_entry = CapabilityHub(tmp_path).get("mcp:papers")
    assert hub_entry["lifecycle"] == "validated"


def test_acquire_http_strips_token(tmp_path):
    fake = _FakeClient([("fetch_page", "Fetch.")])
    result = acquire_mcp_server(
        tmp_path, "web",
        {"transport": "http", "url": "https://example.invalid/mcp",
         "token": "sekrit-token"},
        _approve, client_factory=_factory_for(fake))
    assert result["entry"]["config"]["url"] == "https://example.invalid/mcp"
    assert "sekrit-token" not in json.dumps(result["entry"]["config"])


def test_acquire_rejection_aborts_before_connect(tmp_path):
    seen = []
    with pytest.raises(RuntimeError, match="rejected by approval"):
        acquire_mcp_server(
            tmp_path, "papers",
            {"transport": "stdio", "command": "papers-mcp"},
            _reject, client_factory=_factory_for(_FakeClient(), seen))
    assert seen == []
    assert CapabilityRegistry(tmp_path).get("papers") is None


def test_acquire_refuses_empty_and_unsafe_tools(tmp_path):
    with pytest.raises(RuntimeError, match="no safe tools"):
        acquire_mcp_server(
            tmp_path, "empty",
            {"transport": "stdio", "command": "x"},
            _approve, client_factory=_factory_for(_FakeClient([])))
    fake = _FakeClient([("good_tool", "Good."),
                        ("bad-name!", "Bad."),
                        ("", "Empty.")])
    result = acquire_mcp_server(
        tmp_path, "mixed",
        {"transport": "stdio", "command": "x"},
        _approve, client_factory=_factory_for(fake))
    assert result["tools"] == ["good_tool"]


def test_acquire_test_call_is_explicit_only(tmp_path):
    fake = _FakeClient([("search_papers", "Search.")])
    result = acquire_mcp_server(
        tmp_path, "papers",
        {"transport": "stdio", "command": "papers-mcp"},
        _approve, client_factory=_factory_for(fake),
        test_call={"tool": "search_papers", "args": {"q": "test"}})
    assert fake.calls == [("search_papers", {"q": "test"})]
    assert result["evidence"][-1]["test"] == "call_tool"
    with pytest.raises(ValueError, match="not an offered tool"):
        acquire_mcp_server(
            tmp_path, "other",
            {"transport": "stdio", "command": "papers-mcp"},
            _approve, client_factory=_factory_for(fake),
            test_call={"tool": "nope", "args": {}})


def _acquired(tmp_path, name="papers", tools=(("search_papers", "S."),
                                             ("fetch_page", "F."))):
    fake = _FakeClient(tools)
    acquire_mcp_server(
        tmp_path, name,
        {"transport": "stdio", "command": "papers-mcp"},
        _approve, client_factory=_factory_for(fake))
    return fake


def test_health_healthy_degraded_unavailable(tmp_path):
    _acquired(tmp_path)
    hub = CapabilityHub(tmp_path)
    result = check_mcp_health(
        tmp_path, "papers",
        client_factory=_factory_for(_FakeClient(
            [("search_papers", "S."), ("fetch_page", "F.")])))
    assert result["health"] == "healthy"
    assert hub.get("mcp:papers")["health"] == "healthy"
    result = check_mcp_health(
        tmp_path, "papers",
        client_factory=_factory_for(_FakeClient(
            [("search_papers", "S."), ("new_tool", "N.")])))
    assert result["health"] == "degraded"
    assert "fetch_page" in result["note"] and "new_tool" in result["note"]
    assert hub.get("mcp:papers")["health"] == "degraded"
    result = check_mcp_health(
        tmp_path, "papers",
        client_factory=_factory_for(_FakeClient(fail=True)))
    assert result["health"] == "unavailable"
    assert hub.get("mcp:papers")["health"] == "unavailable"


def test_health_retired_not_probed_and_unknown_raises(tmp_path):
    _acquired(tmp_path)
    CapabilityRegistry(tmp_path).retire("papers", "replaced")
    seen = []
    result = check_mcp_health(
        tmp_path, "papers",
        client_factory=_factory_for(_FakeClient(), seen))
    assert result["health"] == "unknown"
    assert seen == []
    with pytest.raises(ValueError, match="unknown MCP server"):
        check_mcp_health(tmp_path, "nope",
                         client_factory=_factory_for(_FakeClient()))


def test_health_operator_config_and_check_all(tmp_path):
    (tmp_path / "mcp_servers.json").write_text(json.dumps({
        "ops": {"transport": "stdio", "command": "ops-mcp"}}))
    _acquired(tmp_path)
    results = check_all_mcp_health(
        tmp_path,
        client_factory=_factory_for(_FakeClient(
            [("search_papers", "S."), ("fetch_page", "F.")])))
    assert {r["name"] for r in results} == {"papers", "ops"}
    assert all(r["health"] == "healthy" for r in results)
