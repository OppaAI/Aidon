"""A plugin tool that raises must surface as a plugin failure, never
as "unknown tool" (which would send the model off to build a tool
that already exists)."""

import json
from types import SimpleNamespace

import pytest

from src.agentic.loop import GoalRunner
from src.capabilities.plugins import PluginRegistry, plugin_tree_hash


def _runner(tmp_path, functions):
    registry = PluginRegistry(tmp_path)
    plugin_dir = tmp_path / "toolbox" / "p"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / ".adopted").write_text(json.dumps({
        "approved_hash": plugin_tree_hash(plugin_dir)}))
    registry.plugins = {
        "p": SimpleNamespace(
            directory=plugin_dir, functions=functions,
            tool_names=lambda: sorted(functions))
    }

    def _unknown(name, args):
        raise ValueError(f"unknown tool: {name}")

    runner = GoalRunner.__new__(GoalRunner)
    runner.kernel_mode = False
    runner.tools = SimpleNamespace(call=_unknown, names=lambda: [])
    runner.plugins = registry
    runner.extra_tools = {}
    return runner


def _boom(args):
    raise RuntimeError("boom inside plugin")


def test_plugin_error_surfaces_instead_of_unknown_tool(tmp_path):
    runner = _runner(tmp_path, {"boom": _boom})
    with pytest.raises(ValueError) as excinfo:
        runner._call_tool("boom", {})
    assert "plugin tool boom failed" in str(excinfo.value)
    assert "RuntimeError" in str(excinfo.value)
    assert "boom inside plugin" not in str(excinfo.value)
    assert "unknown tool" not in str(excinfo.value)


def test_genuinely_unknown_tool_still_gets_recovery_note(tmp_path):
    runner = _runner(tmp_path, {})
    with pytest.raises(ValueError) as excinfo:
        runner._call_tool("nope", {})
    assert "unknown tool: nope" in str(excinfo.value)


def test_registry_reports_unknown_plugin_tool(tmp_path):
    registry = PluginRegistry(tmp_path)
    with pytest.raises(ValueError, match="unknown plugin tool: nope"):
        registry.call("nope", {})
