"""Gap 1 (last path) + Gap 2 + Gap 6 (registry half):
MCP acquisition governed, learn cycle fail-closed with
recorded learns, registry usage + stale proposals."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.acquisition_governor import (
    AcquisitionBudgetExceeded, AcquisitionGovernor)
from src.capabilities.capability_registry import CapabilityRegistry
from src.capabilities.mcp_acquisition import acquire_mcp_server
from src.capabilities.skill_lifecycle import LifecycleStore


class _FakeMcpClient:
    def __init__(self, tools):
        self._tools = tools

    def list_tools(self):
        return [{"name": name, "description": f"tool {name}"}
                for name in self._tools]

    def close(self):
        pass


def _approved(*args):
    return SimpleNamespace(verdict="approved")


def _rejected(*args):
    return SimpleNamespace(verdict="rejected")


def _acquire(home, name, gov):
    return acquire_mcp_server(
        home, name, {"transport": "stdio", "command": "fake-server"},
        _approved, client_factory=lambda cfg: _FakeMcpClient(["echo"]),
        governor=gov)


def test_mcp_acquisition_counts_and_caps(tmp_path):
    gov = AcquisitionGovernor()
    out = _acquire(tmp_path, "srv-a", gov)
    assert out["entry"]["status"] == "validated"
    assert gov.summary()["mcp"] == 1
    with pytest.raises(AcquisitionBudgetExceeded, match="mcp"):
        _acquire(tmp_path, "srv-b", gov)


def test_mcp_acquisition_rejected_approval_consumes_nothing(tmp_path):
    gov = AcquisitionGovernor()
    with pytest.raises(RuntimeError, match="rejected"):
        acquire_mcp_server(
            tmp_path, "srv-x", {"transport": "stdio", "command": "fake"},
            _rejected, client_factory=lambda cfg: _FakeMcpClient(["echo"]),
            governor=gov)
    assert gov.summary()["mcp"] == 0
    _acquire(tmp_path, "srv-x", gov)
    assert gov.summary()["mcp"] == 1


def test_mcp_acquisition_without_governor_still_works(tmp_path):
    out = acquire_mcp_server(
        tmp_path, "srv-plain", {"transport": "stdio", "command": "fake"},
        _approved, client_factory=lambda cfg: _FakeMcpClient(["echo"]))
    assert out["entry"]["status"] == "validated"


def test_registry_use_count_and_stale(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    evidence = [{"command": "pip show scipy", "passed": True}]
    config = {"name": "scipy-env", "pip": ["scipy"], "apt": [],
              "base": "ubuntu:24.04+py3.12",
              "test_command": "pip show scipy", "test_passed": True}
    entry = registry.register("scipy-env", "image", config, evidence)
    assert entry["use_count"] == 0 and entry["last_used"] == ""
    with pytest.raises(KeyError):
        registry.record_use("nope")
    registry.record_use("scipy-env")
    registry.record_use("scipy-env")
    assert registry.get("scipy-env")["use_count"] == 2
    assert registry.get("scipy-env")["last_used"] != ""
    assert registry.stale_entries(days=90) == []
    # Backdate creation and clear last use: now it is stale.
    path = tmp_path / "capabilities.json"
    data = json.loads(path.read_text())
    data["scipy-env"]["created_at"] = "2020-01-01T00:00:00+00:00"
    data["scipy-env"]["last_used"] = ""
    path.write_text(json.dumps(data))
    stale = registry.stale_entries(days=90)
    assert [e["name"] for e in stale] == ["scipy-env"]
    assert stale[0]["idle_days"] >= 90
    # Using it again clears the stale proposal.
    registry.record_use("scipy-env")
    assert registry.stale_entries(days=90) == []
    registry.retire("scipy-env", "done")
    assert registry.stale_entries(days=0) == []
