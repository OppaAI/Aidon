"""Wiring tests: parity modules are reachable from real entry points,
not just importable in isolation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agentic.research import Researcher
from src.agentic.research_cache import ResearchCache
from src.capabilities.plugins import PluginRegistry
from src.capabilities.security_gate import command_needs_mandatory_approval


def test_researcher_uses_cache(tmp_path):
    cache = ResearchCache(tmp_path)
    cache.store("http://example.com/page", "<p>cached facts here</p>")
    researcher = Researcher(cache=cache)
    source = researcher.fetch("http://example.com/page")
    assert source.error == ""
    assert "cached facts here" in source.excerpt


def test_mandatory_shell_command_is_flagged():
    assert command_needs_mandatory_approval("rm -rf /tmp/x") is not None
    assert command_needs_mandatory_approval("echo hello") is None


def test_plugin_adopt_blocked_by_security_gate(tmp_path):
    plugin_dir = tmp_path / "evil"
    plugin_dir.mkdir()
    # A secret-like string in a neutrally named variable: the
    # pattern scan does not flag it, the security gate does.
    (plugin_dir / "tool.py").write_text(
        'def evil_tool(text):\n    x = "sk-abcdefghij1234567890"\n    return text + x\n',
        encoding="utf-8")
    (plugin_dir / "manifest.json").write_text(
        '{"name": "evil", "version": "0.1.0", "description": "evil", '
        '"tools": ["evil_tool"]}',
        encoding="utf-8")
    registry = PluginRegistry(tmp_path)
    ok, _findings, message = registry.adopt(
        plugin_dir, approve=lambda plugin, f: True)
    assert ok is False
    assert "security gate" in message
