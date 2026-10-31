"""Tier 1 self-development: capability-declared plugin tools.

Pure tools keep ``fn(args)``; a tool with ``"context": true`` gets
``fn(args, ctx)`` and the ctx broker grants only the capabilities the
manifest declared and the owner approved. The posture decides how far
adoption may go, and the approval card still lists every capability.
"""
from tests.plugin_acceptance import passing_backend_factory

import json

import pytest

from pathlib import Path

from src.capabilities.plugin_runtime import (
    CapabilityPolicy, PluginContext, build_context)
from src.capabilities.plugins import (
    PluginRegistry, adoption_gate, load_plugin, scan_plugin)
from src.capabilities.sandbox import Sandbox
from src.agentic.loop import GoalRunner
from src.system.model_client import FakeClient


def _plugin_dir(tmp_path, name="gadget", capabilities=None, code="",
                context_tool=True, tool_name="gadget"):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    tool = {"name": tool_name, "module": "tool.py",
            "function": "gadget_tool"}
    if context_tool:
        tool["context"] = True
    manifest = {
        "name": name, "version": "1.0.0", "description": "capability test",
        "tools": [tool], "skills": [],
        "acceptance_tests": [["python3", "-c", "assert True"]],
    }
    if capabilities is not None:
        manifest["capabilities"] = capabilities
    (directory / "plugin.json").write_text(json.dumps(manifest))
    (directory / "tool.py").write_text(
        code or "def gadget_tool(args, ctx):\n    return 'ok'\n")
    return directory


class _FakeResult:
    def __init__(self, ok=True, stdout="ran", stderr=""):
        self.ok = ok
        self.stdout = stdout
        self.stderr = stderr


class _FakeSandbox:
    def __init__(self):
        self.commands = []

    def run(self, command):
        self.commands.append(list(command))
        return _FakeResult()


def test_policy_parses_capabilities():
    policy = CapabilityPolicy.from_capabilities(
        ["http:example.com,api.test", "fs", "run", "secret:API_KEY"])
    assert policy.http is True
    assert policy.allowed_hosts == ("example.com", "api.test")
    assert policy.fs is True
    assert policy.run is True
    assert policy.secrets == ("API_KEY",)


def test_policy_rejects_unknown_capability():
    with pytest.raises(ValueError):
        CapabilityPolicy.from_capabilities(["netwrok"])


def test_policy_secret_requires_name():
    with pytest.raises(ValueError):
        CapabilityPolicy.from_capabilities(["secret"])


def test_manifest_capabilities_loaded(tmp_path):
    directory = _plugin_dir(tmp_path, capabilities=["fs"])
    plugin = load_plugin(directory)
    assert plugin.capabilities == ["fs"]
    assert plugin.needs_context("gadget") is True


def test_pure_tool_still_one_arg(tmp_path):
    directory = _plugin_dir(tmp_path, capabilities=[], context_tool=False,
                            code="def gadget_tool(args):\n    return args['x'].upper()\n")
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer", test_backend_factory=passing_backend_factory)
    registry.load_all()
    assert registry.call("gadget", {"x": "hi"}) == "HI"


def test_context_tool_fs_is_confined(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(
        tmp_path, capabilities=["fs"],
        code=("def gadget_tool(args, ctx):\n"
              "    ctx.write_file(args['name'], args['text'])\n"
              "    return ctx.read_file(args['name'])\n"))
    registry = PluginRegistry(home)
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer", test_backend_factory=passing_backend_factory)
    registry.load_all()
    result = registry.call(
        "gadget", {"name": "note.txt", "text": "hello"},
        context_factory=lambda plugin: build_context(
            plugin.name, home, plugin.capabilities))
    assert result == "hello"
    assert (home / "plugin_data" / "gadget" / "note.txt").read_text() == "hello"


def test_context_tool_fs_escape_rejected(tmp_path):
    home = tmp_path / "home"
    ctx = PluginContext("gadget", home,
                        CapabilityPolicy.from_capabilities(["fs"]))
    with pytest.raises(ValueError):
        ctx.write_file("../escape.txt", "x")


def test_adopted_plugin_gets_declared_capabilities(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(
        tmp_path, capabilities=["fs"],
        code=("def gadget_tool(args, ctx):\n"
              "    ctx.write_file(args['name'], args['text'])\n"
              "    return ctx.read_file(args['name'])\n"))
    registry = PluginRegistry(home)
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer", test_backend_factory=passing_backend_factory)
    registry.load_all()
    plugin = registry.plugins["gadget"]
    assert (Path(plugin.directory) / ".adopted").is_file()
    broker = registry.context_for("gadget", home)
    assert broker.policy.fs is True


def test_hand_placed_plugin_is_not_imported_or_registered(tmp_path):
    """An unadopted toolbox directory is data, never executable code."""
    home = tmp_path / "home"
    target = home / "toolbox" / "gadget"
    target.mkdir(parents=True)
    sentinel = tmp_path / "imported.txt"
    (target / "plugin.json").write_text(json.dumps({
        "name": "gadget", "version": "1.0.0", "description": "hand placed",
        "capabilities": ["fs"],
        "tools": [{"name": "gadget", "module": "tool.py",
                   "function": "gadget_tool", "context": True}],
    }))
    (target / "tool.py").write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('imported')\n"
        "def gadget_tool(args, ctx):\n"
        "    ctx.write_file('x', 'y')\n"
        "    return 'ok'\n")
    registry = PluginRegistry(home)
    registry.load_all()
    assert registry.plugins == {}
    assert registry.tool_names() == []
    assert not sentinel.exists()


def test_goal_runner_dispatch_refuses_hand_placed_capability(tmp_path):
    home = tmp_path / "home"
    target = home / "toolbox" / "gadget"
    target.mkdir(parents=True)
    (target / "plugin.json").write_text(json.dumps({
        "name": "gadget", "version": "1.0.0", "description": "hand placed",
        "capabilities": ["fs"],
        "tools": [{"name": "gadget", "module": "tool.py",
                   "function": "gadget_tool", "context": True}],
    }))
    (target / "tool.py").write_text(
        "def gadget_tool(args, ctx):\n"
        "    ctx.write_file('x', 'y')\n"
        "    return 'ok'\n")
    registry = PluginRegistry(home)
    registry.load_all()
    runner = GoalRunner(
        FakeClient(""), Sandbox(home / "work"), None, home,
        plugins=registry)
    with pytest.raises(ValueError, match="unknown tool"):
        runner._call_tool("gadget", {})


def test_ctx_secret_requires_declaration(tmp_path):
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities([]))
    with pytest.raises(PermissionError):
        ctx.secret("API_KEY")


def test_ctx_secret_reads_declared_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_TEST_SECRET", "s3cr3t")
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities(
                            ["secret:AIDON_TEST_SECRET"]))
    assert ctx.secret("AIDON_TEST_SECRET") == "s3cr3t"


def test_ctx_http_requires_capability(tmp_path):
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities([]))
    with pytest.raises(PermissionError):
        ctx.http_get("https://example.com")


def test_ctx_http_host_allowlist(tmp_path):
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities(
                            ["http:allowed.example"]))
    with pytest.raises(PermissionError):
        ctx.http_get("https://blocked.example/x")


def test_ctx_http_blocks_private_network_targets(tmp_path):
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities(["http"]))
    with pytest.raises(PermissionError, match="SSRF guard"):
        ctx._checked_url("http://127.0.0.1/admin")


def test_ctx_run_requires_capability(tmp_path):
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities([]))
    with pytest.raises(PermissionError):
        ctx.run(["echo", "hi"])


def test_ctx_run_uses_sandbox(tmp_path):
    sandbox = _FakeSandbox()
    ctx = PluginContext("gadget", tmp_path,
                        CapabilityPolicy.from_capabilities(["run"]),
                        sandbox=sandbox)
    assert ctx.run(["echo", "hi"]) == "ran"
    assert sandbox.commands == [["echo", "hi"]]


def test_context_tool_without_broker_fails_loudly(tmp_path):
    directory = _plugin_dir(tmp_path, capabilities=["fs"])
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer", test_backend_factory=passing_backend_factory)
    registry.load_all()
    with pytest.raises(ValueError):
        registry.call("gadget", {})


def test_adoption_gate_matrix():
    assert adoption_gate("locked", False, [])[0] is False
    assert adoption_gate("standard", True, [])[0] is False
    assert adoption_gate("standard", False, [])[0] is True
    assert adoption_gate("standard", False, ["http"])[0] is False
    assert adoption_gate("developer", False, ["http"])[0] is True
    assert adoption_gate("selfmod", False, ["http", "fs"])[0] is True


def test_scan_still_blocks_raw_subprocess(tmp_path):
    """The broker is the only network/subprocess path: raw imports stay
    blocked so a plugin cannot bypass the capability policy."""
    directory = _plugin_dir(
        tmp_path, capabilities=["http"],
        code=("import subprocess\n"
              "def gadget_tool(args, ctx):\n"
              "    return subprocess.run(['ls'])\n"))
    types = {f.type for f in scan_plugin(directory)}
    assert "subprocess" in types


def test_preflight_card_lists_capabilities(tmp_path):
    from src.capabilities.plugins import preflight_plugin
    directory = _plugin_dir(tmp_path, capabilities=["fs", "secret:TOKEN"])
    preflight = preflight_plugin(directory)
    assert preflight.ok is True
    assert "Capabilities:" in preflight.body
    assert "fs (plugin data dir)" in preflight.body
    assert "secret:TOKEN" in preflight.body
