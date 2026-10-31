"""Fixes for GPT's review of dev 46729da: MCP lifecycle wiring,
adopt preflight/approval/count/commit ordering, builder hard
deadline, YouTube streaming + Range reconciliation, and
governor persistence across resumed runs."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.acquisition_governor import (
    AcquisitionBudgetExceeded, AcquisitionGovernor, budget_key_for,
    discard_budget_record, load_budget_record)
from src.capabilities.capability_registry import CapabilityRegistry
from src.capabilities.mcp_acquisition import acquire_mcp_server
from src.capabilities.modal_images import CapabilityImageBuilder
from src.capabilities.plugins import (
    PluginRegistry, commit_plugin, preflight_plugin)
from src.interface.mode_stops import load_mode_stop, save_mode_stop
from src.system.main import _goal_extra_tools, _make_governor


def _valid_plugin(plug):
    plug.mkdir(parents=True, exist_ok=True)
    (plug / "plugin.json").write_text(json.dumps({
        "name": "five-plug", "version": "1", "description": "test",
        "tools": [{"name": "echo", "module": "tool.py",
                   "function": "echo"}],
        "skills": []}))
    (plug / "tool.py").write_text("def echo(args):\n    return 'hi'\n")


def test_failed_replacement_preserves_installed_plugin(tmp_path, monkeypatch):
    import src.capabilities.plugins as module
    source = tmp_path / "draft"
    _valid_plugin(source)
    registry = PluginRegistry(tmp_path / "home")
    preflight = preflight_plugin(source)
    assert commit_plugin(registry, source, preflight)[0]
    installed = registry.toolbox_dir / "five-plug" / "tool.py"
    original = installed.read_bytes()
    (source / "tool.py").write_text("def echo(args):\n    return 'new'\n")
    preflight = preflight_plugin(source)
    def fail_copy(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(module.shutil, "copytree", fail_copy)
    assert not commit_plugin(registry, source, preflight)[0]
    assert installed.read_bytes() == original


def test_registry_restores_approved_backup_after_interruption(tmp_path):
    source = tmp_path / "draft"
    _valid_plugin(source)
    registry = PluginRegistry(tmp_path / "home")
    assert commit_plugin(registry, source, preflight_plugin(source))[0]
    target = registry.toolbox_dir / "five-plug"
    backup = registry.toolbox_dir / ".backup-interrupted"
    target.rename(backup)
    restored = PluginRegistry(tmp_path / "home")
    restored.load_all()
    assert target.is_dir()
    assert "five-plug" in restored.plugins
    assert restored.tool_names() == ["echo"]


class _ApproveGate:
    def ask(self, kind, title, body, action_class=None, force_ask=False):
        return SimpleNamespace(verdict="approved")


class _RejectGate:
    def ask(self, kind, title, body, action_class=None, force_ask=False):
        return SimpleNamespace(verdict="rejected")


def _tools(tmp_path, gate, governor):
    return _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                             gate=gate, governor=governor)


def test_rejected_adoption_consumes_nothing(tmp_path):
    plug = tmp_path / "plug"
    _valid_plugin(plug)
    gov = AcquisitionGovernor()
    tools = _tools(tmp_path, _RejectGate(), gov)
    result = tools["adopt_tool_plugin"](str(plug))
    assert result.startswith("adoption refused")
    assert gov.tools == 0


def test_approved_adoption_counts_once_then_exhausts(tmp_path):
    plug = tmp_path / "plug"
    _valid_plugin(plug)
    gov = AcquisitionGovernor(max_tools=1)
    tools = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                              gate=_ApproveGate(), governor=gov,
                              optional_tools=False)
    result = tools["adopt_tool_plugin"](str(plug))
    assert result == (
        "Adopted plugin five-plug; tools available after this adoption step "
        "succeeds: echo")
    assert gov.tools == 1
    with pytest.raises(AcquisitionBudgetExceeded, match="tool"):
        tools["adopt_tool_plugin"](str(plug))


def test_adopted_tool_is_live_in_the_same_workflow(tmp_path):
    workdir = tmp_path / "work"
    plug = workdir / "plug"
    _valid_plugin(plug)
    registry = PluginRegistry(tmp_path)
    registry.load_all()
    tools = _goal_extra_tools(
        SimpleNamespace(home=tmp_path), workdir, gate=_ApproveGate(),
        plugin_registry=registry)

    assert tools["adopt_tool_plugin"](str(plug)) == (
        "Adopted plugin five-plug; tools available after this adoption step "
        "succeeds: echo")
    assert "echo" in registry.tool_names()
    assert registry.call("echo", {}) == "hi"


def test_web_adoption_requires_successful_acceptance_tests(tmp_path):
    plug = tmp_path / "plug"
    _valid_plugin(plug)
    class Backend:
        name = "modal"
        allow_network = False
        def run(self, command, workspace, timeout_seconds):
            return SimpleNamespace(isolated=True, ok=True, returncode=0)
        def cleanup(self):
            pass
    tools = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                              gate=_ApproveGate(), test_backend_factory=Backend)
    assert "acceptance tests failed" in tools["adopt_tool_plugin"]("plug")
    manifest = json.loads((plug / "plugin.json").read_text())
    manifest["acceptance_tests"] = [["python3", "test_tool.py"]]
    (plug / "plugin.json").write_text(json.dumps(manifest))
    (plug / "test_tool.py").write_text("assert True\n")
    class Failing(Backend):
        def run(self, command, workspace, timeout_seconds):
            return SimpleNamespace(isolated=True, ok=False, returncode=1)
    failing_tools = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                                      gate=_ApproveGate(), test_backend_factory=Failing)
    assert "acceptance tests failed" in failing_tools["adopt_tool_plugin"]("plug")
    assert not (tmp_path / "toolbox" / "five-plug").exists()
    assert tools["adopt_tool_plugin"]("plug").startswith("Adopted plugin")


def test_configured_cli_adoption_fails_closed_without_isolated_backend(tmp_path):
    from src.system.config import Config
    plug = tmp_path / "plug"
    _valid_plugin(plug)
    manifest = json.loads((plug / "plugin.json").read_text())
    manifest["acceptance_tests"] = [["python3", "test_tool.py"]]
    (plug / "plugin.json").write_text(json.dumps(manifest))
    (plug / "test_tool.py").write_text("assert True\n")
    config = Config(home=tmp_path, execution_backend="bubblewrap")
    tools = _goal_extra_tools(config, tmp_path, gate=_ApproveGate())
    assert "acceptance tests failed" in tools["adopt_tool_plugin"]("plug")
    assert not (tmp_path / "toolbox" / "five-plug").exists()


def test_commit_rejects_changed_source(tmp_path):
    home = tmp_path / "home"
    plug = tmp_path / "plug"
    _valid_plugin(plug)
    preflight = preflight_plugin(plug)
    assert preflight.ok
    (plug / "tool.py").write_text("def echo(args):\n    return 'changed'\n")
    ok, _findings, message = commit_plugin(
        PluginRegistry(home), plug, preflight)
    assert ok is False
    assert "changed after approval" in message
    assert not (home / "toolbox" / "five-plug").exists()


class _FakeMcpClient:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def start(self):
        return None

    def list_tools(self):
        return [{"name": "lookup", "description": "look things up"}]

    def call_tool(self, name, args):
        return "{}"

    def close(self):
        self.closed = True


def _approve(kind, title, body):
    return SimpleNamespace(verdict="approved")


def test_acquire_keep_client_and_reacquire(tmp_path):
    clients = []

    def factory(config):
        client = _FakeMcpClient()
        clients.append(client)
        return client

    config = {"transport": "stdio", "command": "fake-bin", "args": []}
    first = acquire_mcp_server(tmp_path, "srv", config, _approve,
                               client_factory=factory, keep_client=True)
    assert first["already_registered"] is False
    assert first["tools"] == ["lookup"]
    assert clients[0].closed is False
    entry = CapabilityRegistry(tmp_path).get("srv")
    assert entry["status"] == "validated"
    assert "fake-bin" in json.dumps(entry["config"])
    second = acquire_mcp_server(tmp_path, "srv", config, _approve,
                                client_factory=factory, keep_client=True)
    assert second["already_registered"] is True
    assert second["entry"]["status"] == "validated"
    with pytest.raises(ValueError, match="different config"):
        acquire_mcp_server(tmp_path, "srv",
                           {"transport": "stdio", "command": "other-bin",
                            "args": []},
                           _approve, client_factory=factory)
    plain = acquire_mcp_server(tmp_path, "srv2", config, _approve,
                               client_factory=factory)
    assert "client" not in plain
    assert clients[-1].closed is True


def test_mcp_connect_registers_and_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr("src.interface.mcp_client.McpClient", _FakeMcpClient)
    gov = AcquisitionGovernor()
    tools = _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                              gate=_ApproveGate(), governor=gov,
                              optional_tools=False)
    result = tools["mcp_connect"]("srv", command="fake-bin")
    assert result.startswith("MCP_CONNECTED")
    assert "lookup" in result
    assert tools["mcp_srv_lookup"]({}) == '"{}"'
    assert CapabilityRegistry(tmp_path).get("srv")["status"] == "validated"
    assert tools["mcp_connect"]("srv", command="fake-bin").startswith(
        "MCP_ALREADY_CONNECTED")
    assert tools["mcp_disconnect"]("srv").startswith("MCP_DISCONNECTED")
    assert "mcp_srv_lookup" not in tools
    # A later run reconnects through the lifecycle instead of
    # failing on the existing record.
    gov2 = AcquisitionGovernor()
    tools2 = _tools(tmp_path, _ApproveGate(), gov2)
    again = tools2["mcp_connect"]("srv", command="fake-bin")
    assert again.startswith("MCP_CONNECTED")


class _Proc:
    def __init__(self, out=""):
        self.stdout = SimpleNamespace(read=lambda: out)
        self.stderr = SimpleNamespace(read=lambda: "")
        self.returncode = 0

    def wait(self):
        return 0


class _TimeoutSandbox:
    def __init__(self):
        self.timeouts = []
        self.terminated = False

    def exec(self, *cmd, timeout=None):
        self.timeouts.append(timeout)
        if "freeze" in cmd:
            return _Proc("requests==2.32.3\n")
        return _Proc()

    def snapshot_filesystem(self):
        return "snapshot-image"

    def terminate(self):
        self.terminated = True


class _TimeoutFactory:
    def __init__(self):
        self.sandboxes = []

    def __call__(self, network, image):
        sb = _TimeoutSandbox()
        self.sandboxes.append(sb)
        return sb


def _builder(tmp_path, governor, setup_timeout=1200, test_timeout=300):
    factory = _TimeoutFactory()
    builder = CapabilityImageBuilder(
        tmp_path, sandbox_factory=factory, governor=governor,
        setup_timeout=setup_timeout, test_timeout=test_timeout)
    builder._base_image = lambda: "base-image"
    return builder, factory


def test_builder_deadline_clamps_timeouts(tmp_path):
    gov = AcquisitionGovernor(max_acquisition_seconds=10.0)
    builder, factory = _builder(tmp_path, gov)
    record = builder.build("img", pip_packages=["requests"])
    assert record["record"]["name"] == "img"
    timeouts = [t for sb in factory.sandboxes for t in sb.timeouts]
    assert timeouts and max(timeouts) <= 10


def test_builder_without_governor_keeps_timeouts(tmp_path):
    builder, factory = _builder(tmp_path, None)
    builder.build("img", pip_packages=["requests"])
    timeouts = [t for sb in factory.sandboxes for t in sb.timeouts]
    assert 1200 in timeouts and 300 in timeouts


def test_builder_exhausted_time_budget_raises(tmp_path):
    gov = AcquisitionGovernor(max_acquisition_seconds=10.0)
    gov.acquisition_seconds = 10.0
    builder, _factory = _builder(tmp_path, gov)
    with pytest.raises(AcquisitionBudgetExceeded, match="time budget"):
        builder.build("img", pip_packages=["requests"])


def test_governor_persistence_roundtrip(tmp_path):
    key = "mission:test123"
    gov = AcquisitionGovernor(home=tmp_path, budget_key=key)
    gov.acquire("tool")
    gov.acquire("mcp")
    resumed = _make_governor(SimpleNamespace(home=tmp_path), key)
    assert (resumed.tools, resumed.mcp) == (1, 1)
    assert load_budget_record(tmp_path, key)["tools"] == 1
    discard_budget_record(tmp_path, key)
    assert load_budget_record(tmp_path, key) is None
    fresh = _make_governor(SimpleNamespace(home=tmp_path), key)
    assert (fresh.tools, fresh.mcp) == (0, 0)


def test_governor_without_key_writes_nothing(tmp_path):
    gov = AcquisitionGovernor(home=tmp_path)
    gov.acquire("tool")
    assert not (tmp_path / "acquisition_budgets.json").exists()


def test_governor_corrupt_record_tolerated(tmp_path):
    (tmp_path / "acquisition_budgets.json").write_text("not json")
    gov = _make_governor(SimpleNamespace(home=tmp_path), "mission:bad")
    assert gov.tools == 0
    assert budget_key_for("chat", "goal").startswith("mission:")


def test_mode_stop_carries_budget_key(tmp_path):
    stop_id = save_mode_stop(tmp_path, mode="plan", goal="g",
                             plan={"steps": []}, budget_key="mission:xyz")
    record = load_mode_stop(tmp_path, stop_id)
    assert record["budget_key"] == "mission:xyz"
