"""Sixth-audit fixes, part D: plugin adopt binding/cap and offline hint.

- Adoption is bound to the approved bytes: a tree hash computed
  before the approval card is shown on the card and re-checked
  immediately before the copy; a changed source aborts adoption.
- Adoption rejects a source tree over MAX_PLUGIN_BYTES, before
  any approval card.
- A security gate that throws now rejects adoption instead of
  presenting a broken scanner as a clean scan; the per-file
  OSError continue is unchanged.
- A failed shell tool in a network-less sandbox keeps its
  "shell failed:" prefix and stderr cap but appends an offline
  hint; with network True or unknown the message is unchanged.
"""

import json
from types import SimpleNamespace

import pytest

from src.capabilities import security_gate
from src.capabilities.plugins import (
    MAX_PLUGIN_BYTES,
    PluginRegistry,
    plugin_tree_hash,
    plugin_tree_size,
)
from src.capabilities.tools import ToolRegistry
from src.interface.approval import Decision


def _plugin_dir(tmp_path, name="upper", code=None):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": []}), encoding="utf-8")
    (directory / "tool.py").write_text(
        code or "def upper(args):\n    return str(args.get('text', '')).upper()\n",
        encoding="utf-8")
    return directory


class _CapturingGate:
    """Approval gate stand-in that records the card body."""

    def __init__(self):
        self.bodies = []

    def ask(self, kind, title, body):
        self.bodies.append(body)
        return Decision("approved")


def test_tree_hash_is_deterministic_and_covers_all_files(tmp_path):
    directory = _plugin_dir(tmp_path)
    (directory / "notes.md").write_text("hello\n", encoding="utf-8")
    (directory / "sub").mkdir()
    (directory / "sub" / "deep.txt").write_text("deep\n", encoding="utf-8")
    first = plugin_tree_hash(directory)
    assert first == plugin_tree_hash(directory)
    (directory / "sub" / "deep.txt").write_text("changed\n", encoding="utf-8")
    assert plugin_tree_hash(directory) != first
    # An added file the copy would copy must change the hash too.
    (directory / "extra.txt").write_text("new\n", encoding="utf-8")
    assert plugin_tree_hash(directory) != first
    assert plugin_tree_size(directory) > 0


def test_card_shows_approved_hash_and_rehash_matches(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    gate = _CapturingGate()
    ok, _, _ = registry.adopt(directory, gate=gate)
    assert ok is True
    assert len(gate.bodies) == 1
    assert plugin_tree_hash(directory) in gate.bodies[0]
    assert "hash" in gate.bodies[0].lower()


def test_source_changed_during_approval_aborts_and_keeps_old_install(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    # First adopt a known-good version.
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True)
    assert ok is True
    installed = (home / "plugins" / "upper" / "tool.py").read_text(encoding="utf-8")

    def approve_and_mutate(plugin, findings):
        # The owner approves, but the source changes before the copy.
        (directory / "tool.py").write_text(
            "def upper(args):\n    return 'MUTATED'\n", encoding="utf-8")
        return True

    ok, _, message = registry.adopt(directory, approve=approve_and_mutate)
    assert ok is False
    assert "changed after approval" in message
    assert "hash" in message.lower()
    # The abort happens before any rmtree: the old install survives.
    assert (home / "plugins" / "upper" / "tool.py").read_text(encoding="utf-8") == installed
    assert registry.call("upper", {"text": "hi"}) != "MUTATED"


def test_size_cap_rejects_before_approval_card(tmp_path):
    assert MAX_PLUGIN_BYTES == 10 * 1024 * 1024
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    (directory / "blob.bin").write_bytes(b"x" * (MAX_PLUGIN_BYTES + 1))
    called = []
    ok, _, message = registry.adopt(
        directory, approve=lambda plugin, f: called.append(True) or True)
    assert ok is False
    assert "over" in message and "cap" in message
    assert called == [], "oversized source must not reach an approval card"
    assert not (home / "plugins" / "upper").exists()


def test_gate_throwing_rejects_instead_of_clean_scan(tmp_path, monkeypatch):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)

    def broken_gate(source, text, client=None):
        raise RuntimeError("scanner exploded")

    monkeypatch.setattr(security_gate, "gate_text", broken_gate)
    called = []
    ok, _, message = registry.adopt(
        directory, approve=lambda plugin, f: called.append(True) or True)
    assert ok is False
    assert "security gate error" in message
    assert "scanner exploded" in message
    assert called == [], "a failed gate must never reach an approval card"
    assert not (home / "plugins" / "upper").exists()


def test_gate_per_file_oserror_still_skips_and_adopts(tmp_path, monkeypatch):
    """The inner per-file OSError continue is unchanged behaviour."""
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)

    def unreadable_gate(source, text, client=None):
        raise OSError("cannot read")

    monkeypatch.setattr(security_gate, "gate_text", unreadable_gate)
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True)
    assert ok is True
    assert (home / "plugins" / "upper" / "plugin.json").exists()


class _FailingSandbox:
    def __init__(self, stderr):
        self.stderr = stderr

    def run(self, command):
        return SimpleNamespace(ok=False, stderr=self.stderr, stdout="")


def _shell_error(tmp_path, network_available, stderr="curl: could not resolve host"):
    work = tmp_path / "work"
    kwargs = {} if network_available is None else {
        "network_available": network_available}
    registry = ToolRegistry(work, sandbox=_FailingSandbox(stderr), **kwargs)
    with pytest.raises(ValueError) as excinfo:
        registry.call("shell", {"command": ["curl", "https://example.invalid"]})
    return str(excinfo.value)


def test_offline_shell_failure_gets_hint_but_keeps_prefix_and_cap(tmp_path):
    long_stderr = "e" * 300
    message = _shell_error(tmp_path, False, stderr=long_stderr)
    assert message.startswith("shell failed: ")
    assert "e" * 200 in message
    assert "e" * 201 not in message, "stderr stays capped at 200 chars"
    assert "no network" in message
    assert "web_search" in message
    assert "fetch_url" in message


def test_shell_failure_unchanged_when_network_true_or_unknown(tmp_path):
    for network_available in (True, None):
        message = _shell_error(tmp_path, network_available,
                               stderr="boom: something broke")
        assert message == "shell failed: boom: something broke"


def test_tool_registry_default_construction_still_works(tmp_path):
    # Existing constructions (no network_available arg) must keep working.
    registry = ToolRegistry(tmp_path / "work")
    assert registry.network_available is None
    assert registry.call("write", {"file": "a.txt", "text": "hi"}) == "wrote a.txt"
