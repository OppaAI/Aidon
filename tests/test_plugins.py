"""#4 plugin system tests: loader, scan, adopt, registry, loop dispatch.

Plugins live in Aidon home, never in the repo. A plugin
with a blocking scan finding is rejected even if the
owner would approve. A plugin with only warnings is
adopted only after approval, with warnings on the card.
"""

import json

from src.interface.approval import ApprovalGate, Decision
from src.agentic.loop import GoalRunner
from src.system.model_client import FakeClient
from src.capabilities.plugins import PluginRegistry, load_plugin, scan_plugin
from src.capabilities.sandbox import Sandbox


def _plugin_dir(tmp_path, name="upper", code=None, skills=None):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": skills or []}), encoding="utf-8")
    (directory / "tool.py").write_text(
        code or "def upper(args):\n    return str(args.get('text', '')).upper()\n",
        encoding="utf-8")
    if skills:
        for skill in skills:
            (directory / skill).write_text("# Skill\nFollow this.\n", encoding="utf-8")
    return directory


def test_load_and_call_plugin(tmp_path):
    directory = _plugin_dir(tmp_path)
    plugin = load_plugin(directory)
    assert plugin.tool_names() == ["upper"]
    assert plugin.functions["upper"]({"text": "hi"}) == "HI"


def test_scan_blocks_eval_and_secret(tmp_path):
    directory = _plugin_dir(tmp_path, code=(
        "api_key = 'should-not-appear'\n"
        "def upper(args):\n    return eval(args.get('text', ''))\n"))
    findings = scan_plugin(directory)
    types = {f.type for f in findings}
    assert "secret" in types
    assert "eval_exec" in types
    # Scan reports path/line/type only, never the value.
    assert "should-not-appear" not in " ".join(
        f"{f.path} {f.type} {f.message}" for f in findings)


def test_adopt_rejects_blocking_even_if_approved(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path, code=(
        "def upper(args):\n    return eval(args.get('text', ''))\n"))
    ok, findings, message = registry.adopt(
        directory, approve=lambda plugin, f: True)
    assert ok is False
    assert not (home / "plugins" / "upper").exists()


def test_adopt_copies_after_approval_and_registry_loads(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path, skills=["skill.md"])
    ok, findings, message = registry.adopt(
        directory, approve=lambda plugin, f: True)
    assert ok is True
    assert (home / "plugins" / "upper" / "plugin.json").exists()
    registry2 = PluginRegistry(home)
    registry2.load_all()
    assert registry2.call("upper", {"text": "hi"}) == "HI"
    assert "upper/skill.md" in registry2.skill_texts()


def test_adopt_without_approval_does_not_copy(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: False)
    assert ok is False
    assert not (home / "plugins" / "upper").exists()


def test_loop_dispatches_to_plugin_tool(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    registry.adopt(_plugin_dir(tmp_path), approve=lambda plugin, f: True)
    registry.load_all()
    plan = {"steps": [
        {"tool": "upper", "args": {"text": "hi"}, "deliverable": ""},
        {"tool": "write", "args": {"file": "note.md", "text": "done"},
         "deliverable": "note.md"}],
        "deliverables": ["note.md"], "criteria": []}
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(json.dumps(plan)), Sandbox(home / "work"),
                        gate, home, memory=None, plugins=registry)
    report = runner.run("Write a note")
    assert report.status == "done"
    assert report.files == ["note.md"]
