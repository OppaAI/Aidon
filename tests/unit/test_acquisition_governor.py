"""Acquisition governor: per-run caps across acquisition
kinds, a shared time budget, and the tool-closure wiring."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.acquisition_governor import (
    AcquisitionBudgetExceeded, AcquisitionGovernor)
from src.capabilities.modal_images import CapabilityImageBuilder
from src.system.main import _goal_extra_tools
from tests.plugin_acceptance import passing_backend_factory


def test_defaults_and_summary():
    gov = AcquisitionGovernor()
    assert (gov.max_tools, gov.max_skills, gov.max_mcp,
            gov.max_dependency_sets, gov.max_repair_cycles,
            gov.max_acquisition_seconds) == (2, 2, 1, 2, 3, 600.0)
    gov.acquire("tool")
    assert gov.summary()["tools"] == 1


def test_each_kind_caps_and_names_the_kind():
    gov = AcquisitionGovernor()
    gov.acquire("mcp")
    with pytest.raises(AcquisitionBudgetExceeded) as exc:
        gov.acquire("mcp")
    assert "mcp" in str(exc.value) and "1 of 1" in str(exc.value)
    gov2 = AcquisitionGovernor()
    gov2.acquire("tool")
    gov2.acquire("tool")
    with pytest.raises(AcquisitionBudgetExceeded, match="tool"):
        gov2.acquire("tool")


def test_failed_attempt_still_consumes_budget():
    gov = AcquisitionGovernor(max_mcp=1)
    with gov.timed("mcp"):
        pass
    with pytest.raises(AcquisitionBudgetExceeded, match="mcp"):
        gov.acquire("mcp")


def test_time_budget_and_timed_accounting():
    gov = AcquisitionGovernor(max_acquisition_seconds=10.0)
    gov.acquisition_seconds = 10.0
    with pytest.raises(AcquisitionBudgetExceeded,
                       match="time budget exhausted"):
        gov.acquire("tool")
    gov2 = AcquisitionGovernor()
    before = gov2.acquisition_seconds
    with gov2.timed("tool"):
        pass
    assert gov2.acquisition_seconds >= before


def test_unknown_kind_rejected():
    with pytest.raises(ValueError, match="unknown acquisition kind"):
        AcquisitionGovernor().acquire("teleport")


class _ApproveGate:
    def ask(self, kind, title, body, action_class=None, force_ask=False):
        return SimpleNamespace(verdict="approved")


def _tools(tmp_path, governor):
    config = SimpleNamespace(home=tmp_path)
    return _goal_extra_tools(config, tmp_path, gate=_ApproveGate(),
                             governor=governor,
                             test_backend_factory=passing_backend_factory)


def test_mcp_connect_wiring_counts_and_caps(tmp_path):
    gov = AcquisitionGovernor(max_mcp=0)
    tools = _tools(tmp_path, gov)
    with pytest.raises(AcquisitionBudgetExceeded, match="mcp"):
        tools["mcp_connect"]("x", command="definitely-not-a-real-binary-xyz")
    gov2 = AcquisitionGovernor()
    tools2 = _tools(tmp_path, gov2)
    result = tools2["mcp_connect"](
        "x", command="definitely-not-a-real-binary-xyz")
    assert result.startswith("MCP_REFUSED")
    assert gov2.mcp == 1


def test_adopt_and_install_wiring_caps(tmp_path):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    gov = AcquisitionGovernor(max_tools=0, max_dependency_sets=0)
    tools = _tools(tmp_path, gov)
    # A preflight refusal costs nothing, even with no budget left.
    assert tools["adopt_tool_plugin"](str(plugin_dir)).startswith(
        "adoption refused")
    assert gov.tools == 0
    # A valid, approved plugin reaches the count and exhausts it.
    (plugin_dir / "plugin.json").write_text(json.dumps({
        "name": "cap-plug", "version": "1", "description": "test",
        "tools": [{"name": "echo", "module": "tool.py",
                   "function": "echo"}],
        "skills": [],
        "acceptance_tests": [["python3", "-c", "assert True"]]}))
    (plugin_dir / "tool.py").write_text("def echo(args):\n    return 'hi'\n")
    with pytest.raises(AcquisitionBudgetExceeded, match="tool"):
        tools["adopt_tool_plugin"](str(plugin_dir))
    with pytest.raises(AcquisitionBudgetExceeded, match="dependency"):
        tools["install_python_package"](["requests"])


class _FakeProc:
    def __init__(self):
        self.stdout = SimpleNamespace(read=lambda: "")
        self.stderr = SimpleNamespace(read=lambda: "")
        self.returncode = 0

    def wait(self):
        return 0


class _FakeSandbox:
    def __init__(self):
        self.terminated = False

    def exec(self, *cmd, timeout=None):
        return _FakeProc()

    def snapshot_filesystem(self):
        return "snapshot-image"

    def terminate(self):
        self.terminated = True


def test_builder_governor_counts_and_caps(tmp_path):
    calls = []
    factory = lambda network, image: (calls.append((network, image)),
                                      _FakeSandbox())[1]
    gov = AcquisitionGovernor(max_dependency_sets=0)
    builder = CapabilityImageBuilder(tmp_path, sandbox_factory=factory,
                                     governor=gov)
    builder._base_image = lambda: "base-image"
    with pytest.raises(AcquisitionBudgetExceeded, match="dependency"):
        builder.build("img", pip_packages=["requests"])
    assert calls == []
    gov2 = AcquisitionGovernor()
    builder2 = CapabilityImageBuilder(tmp_path, sandbox_factory=factory,
                                      governor=gov2)
    builder2._base_image = lambda: "base-image"
    builder2.build("img", pip_packages=["requests"])
    assert gov2.dependency_sets == 1
