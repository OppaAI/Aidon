"""Tier 2: sandboxed process tools.

A ``"mode": "sandbox"`` tool runs as an isolated subprocess: the module
is imported inside the runner, never in Aidon's process, so
subprocess/network/eval in tool code stay contained. Args and approved
secrets travel on stdin; the tool is adoption-gated like any capability
plugin.
"""

import json
import os
import shutil

import pytest

from pathlib import Path

from src.capabilities.plugin_runtime import run_sandbox_tool
from src.capabilities.plugins import (
    PluginRegistry, adoption_gate, load_plugin, preflight_plugin,
    sandbox_module_paths, scan_plugin)
from src.capabilities.sandbox import Sandbox


def _sandbox_plugin(tmp_path, code, capabilities=None, name="heavy",
                    extra_tools=None, module="tool.py"):
    directory = tmp_path / "draft" / name
    directory.mkdir(parents=True, exist_ok=True)
    tools = [{"name": name, "module": module, "function": "heavy",
              "mode": "sandbox"}]
    tools.extend(extra_tools or [])
    manifest = {"name": name, "version": "1.0.0",
                "description": "sandbox tool test", "tools": tools}
    if capabilities is not None:
        manifest["capabilities"] = capabilities
    (directory / "plugin.json").write_text(json.dumps(manifest))
    (directory / module).write_text(code)
    return directory


def _types(findings):
    return {f.type for f in findings}


def test_load_never_imports_sandbox_module(tmp_path):
    marker = tmp_path / "imported.txt"
    code = (f"from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ran')\n"
            "def heavy(args):\n    return 'ok'\n")
    directory = _sandbox_plugin(tmp_path, code)
    plugin = load_plugin(directory)  # execute=True by default
    assert plugin.tool_names() == ["heavy"]
    assert "heavy" not in plugin.functions, "sandbox code must not run in-process"
    assert not marker.exists()


def test_scan_downgrades_sandbox_execution_patterns(tmp_path):
    code = ("import subprocess\nimport urllib.request\n"
            "def heavy(args):\n"
            "    subprocess.run(['ls'])\n"
            "    return urllib.request.urlopen('https://x').read()\n")
    directory = _sandbox_plugin(tmp_path, code)
    types = _types(scan_plugin(directory))
    assert "sandbox_subprocess" in types
    assert "sandbox_network" in types
    assert "subprocess" not in types
    assert "network" not in types


def test_shared_module_with_inline_tool_is_not_exempt(tmp_path):
    code = "import subprocess\ndef heavy(args):\n    return subprocess.run(['ls'])\n"
    directory = _sandbox_plugin(
        tmp_path, code,
        extra_tools=[{"name": "inline", "module": "tool.py",
                      "function": "heavy"}])
    assert sandbox_module_paths(directory) == set()
    assert "subprocess" in _types(scan_plugin(directory))


def test_preflight_ok_and_card_lists_sandbox_tools(tmp_path):
    code = "import subprocess\ndef heavy(args):\n    return 'ok'\n"
    directory = _sandbox_plugin(tmp_path, code, capabilities=["network"])
    pre = preflight_plugin(directory)
    assert pre.ok is True
    assert "Sandbox tools" in pre.body
    assert "network (sandbox only)" in pre.body


def test_adoption_gate_refuses_sandbox_under_standard():
    assert adoption_gate("standard", False, [], sandboxed=True)[0] is False
    assert adoption_gate("developer", False, [], sandboxed=True)[0] is True
    assert adoption_gate("selfmod", False, [], sandboxed=True)[0] is True


def test_sandbox_tool_requires_adoption(tmp_path):
    code = "def heavy(args):\n    return 'ok'\n"
    directory = _sandbox_plugin(tmp_path, code)
    # Hand place: no .adopted marker.
    target = tmp_path / "home" / "toolbox" / "heavy"
    target.parent.mkdir(parents=True)
    target.mkdir()
    (target / "plugin.json").write_text((directory / "plugin.json").read_text())
    (target / "tool.py").write_text(code)
    registry = PluginRegistry(tmp_path / "home")
    registry.load_all()
    with pytest.raises(ValueError, match="unknown plugin tool"):
        registry.call("heavy", {}, sandbox=Sandbox(tmp_path / "work"))


def test_registry_call_requires_sandbox(tmp_path):
    code = "def heavy(args):\n    return 'ok'\n"
    directory = _sandbox_plugin(tmp_path, code)
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer")
    registry.load_all()
    with pytest.raises(ValueError, match="no sandbox"):
        registry.call("heavy", {})


@pytest.mark.skipif(shutil.which("bwrap") is None,
                    reason="bubblewrap is required for sandbox plugin tests")
def test_run_sandbox_tool_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_T2_SECRET", "tier2-secret")
    code = (
        "import os\n"
        "def heavy(args):\n"
        "    return f\"{args['city']}:{os.getpid()}:{os.environ.get('AIDON_T2_SECRET', '')}\"\n"
    )
    directory = _sandbox_plugin(
        tmp_path, code, capabilities=["secret:AIDON_T2_SECRET"])
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer")
    registry.load_all()
    template = Sandbox(tmp_path / "template_work")
    result = registry.call("heavy", {"city": "Kyiv"}, sandbox=template)
    city, pid, secret = result.split(":")
    assert city == "Kyiv"
    assert int(pid) != os.getpid(), "tool must run in a separate process"
    assert secret == "tier2-secret"


@pytest.mark.skipif(shutil.which("bwrap") is None,
                    reason="bubblewrap is required for sandbox plugin tests")
def test_run_sandbox_tool_reports_plugin_error(tmp_path):
    code = "def heavy(args):\n    raise RuntimeError('boom')\n"
    directory = _sandbox_plugin(tmp_path, code)
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer")
    registry.load_all()
    with pytest.raises(RuntimeError, match="boom"):
        registry.call("heavy", {}, sandbox=Sandbox(tmp_path / "work"))


@pytest.mark.skipif(shutil.which("bwrap") is None,
                    reason="bubblewrap is required for sandbox plugin tests")
def test_run_sandbox_tool_writes_no_result_outside_run_dir(tmp_path):
    """The runner's result file lives in the run dir, not the caller's
    cwd, and args never appear on the host command line."""
    code = "def heavy(args):\n    return args['payload']\n"
    directory = _sandbox_plugin(tmp_path, code)
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer")
    registry.load_all()
    result = registry.call("heavy", {"payload": "hello"},
                           sandbox=Sandbox(tmp_path / "work"))
    assert result == "hello"
    assert (tmp_path / "home" / "plugin_runs" / "heavy").is_dir()


@pytest.mark.skipif(shutil.which("bwrap") is None,
                    reason="bubblewrap is required for sandbox isolation")
def test_sandbox_plugin_cannot_write_outside_its_run_dir(tmp_path):
    code = (
        "from pathlib import Path\n"
        "def heavy(args):\n"
        "    try:\n"
        "        Path(args['host_path']).write_text('escaped')\n"
        "    except OSError:\n"
        "        return 'blocked'\n"
        "    return 'wrote'\n")
    directory = _sandbox_plugin(tmp_path, code)
    registry = PluginRegistry(tmp_path / "home")
    registry.adopt(directory, approve=lambda plugin, findings: True,
                   tool_posture="developer")
    registry.load_all()
    host_path = tmp_path / "outside.txt"
    result = registry.call(
        "heavy", {"host_path": str(host_path)},
        sandbox=Sandbox(tmp_path / "template"))
    assert result == "blocked"
    assert not host_path.exists()
