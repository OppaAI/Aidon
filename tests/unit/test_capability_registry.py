"""Capability registry: evidence-gated registration,
approval-gated promotion, safe-direction flag/retire,
and name uniqueness."""

from types import SimpleNamespace

import pytest

from src.capabilities.capability_registry import (
    CapabilityRegistry, image_evidence)


def _approved(kind, title, body):
    return SimpleNamespace(verdict="approved")


def _rejected(kind, title, body):
    return SimpleNamespace(verdict="rejected")


def _evidence():
    return [{"command": "pip show scipy", "passed": True,
             "detail": "locked sandbox test passed at build"}]


def _image_config():
    return {"name": "scipy-env", "pip": ["scipy"], "apt": [],
            "base": "ubuntu:24.04+py3.12",
            "test_command": "pip show scipy", "test_passed": True}


def _mcp_config():
    return {"transport": "stdio", "command": "papers-server",
            "args": ["--port", "8000"], "tools": ["search", "cite"]}


def test_register_validated_and_persisted(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    entry = registry.register("scipy-env", "image", _image_config(),
                              _evidence())
    assert entry["status"] == "validated"
    assert entry not in registry.usable()
    fresh = CapabilityRegistry(tmp_path)
    assert fresh.get("scipy-env")["kind"] == "image"
    assert [e["name"] for e in fresh.list(kind="image")] == ["scipy-env"]


def test_register_refuses_failing_or_empty_evidence(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    bad = [{"command": "pip show scipy", "passed": False,
            "detail": "failed"}]
    with pytest.raises(ValueError, match="did not all pass"):
        registry.register("scipy-env", "image", _image_config(), bad)
    with pytest.raises(ValueError, match="untested"):
        registry.register("scipy-env", "image", _image_config(), [])
    assert registry.get("scipy-env") is None


def test_register_refuses_duplicate_until_retired(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    registry.register("scipy-env", "image", _image_config(), _evidence())
    with pytest.raises(ValueError, match="already registered"):
        registry.register("scipy-env", "image", _image_config(),
                          _evidence())
    registry.retire("scipy-env", "superseded")
    entry = registry.register("scipy-env", "image", _image_config(),
                              _evidence())
    assert entry["status"] == "validated"


def test_promote_needs_approval(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    registry.register("scipy-env", "image", _image_config(), _evidence())
    with pytest.raises(RuntimeError, match="approval"):
        registry.promote("scipy-env", None)
    with pytest.raises(RuntimeError, match="rejected"):
        registry.promote("scipy-env", _rejected)
    assert registry.get("scipy-env")["status"] == "validated"
    entry = registry.promote("scipy-env", _approved)
    assert entry["status"] == "trusted"
    assert [e["name"] for e in registry.usable()] == ["scipy-env"]


def test_flag_and_retire_safe_directions(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    registry.register("scipy-env", "image", _image_config(), _evidence())
    registry.promote("scipy-env", _approved)
    flagged = registry.flag("scipy-env", "test failed in maintenance")
    assert flagged["status"] == "validated"
    assert registry.usable() == []
    registry.promote("scipy-env", _approved)
    retired = registry.retire("scipy-env", "no longer needed")
    assert retired["status"] == "retired"
    with pytest.raises(ValueError, match="not validated"):
        registry.promote("scipy-env", _approved)


def test_mcp_config_validated(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    bad_stdio = {"transport": "stdio", "tools": ["search"]}
    with pytest.raises(ValueError, match="command"):
        registry.register("papers", "mcp", bad_stdio, _evidence())
    bad_http = {"transport": "http", "tools": ["search"]}
    with pytest.raises(ValueError, match="url"):
        registry.register("papers", "mcp", bad_http, _evidence())
    no_tools = {"transport": "stdio", "command": "papers-server",
                "tools": []}
    with pytest.raises(ValueError, match="tool"):
        registry.register("papers", "mcp", no_tools, _evidence())
    entry = registry.register("papers", "mcp", _mcp_config(), _evidence())
    assert entry["status"] == "validated"


def test_invalid_names_and_kinds_rejected(tmp_path):
    registry = CapabilityRegistry(tmp_path)
    with pytest.raises(ValueError, match="Invalid capability name"):
        registry.register("Bad Name!", "image", _image_config(),
                          _evidence())
    with pytest.raises(ValueError, match="unknown capability kind"):
        registry.register("x", "plugin", {}, _evidence())


def test_image_evidence_helper(tmp_path):
    evidence = image_evidence(_image_config())
    assert evidence[0]["passed"] is True
    assert evidence[0]["command"] == "pip show scipy"
    registry = CapabilityRegistry(tmp_path)
    entry = registry.register("scipy-env", "image", _image_config(),
                              evidence)
    assert entry["status"] == "validated"
