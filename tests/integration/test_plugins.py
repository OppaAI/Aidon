"""#4 plugin system tests: loader, scan, adopt, registry, loop dispatch.

Plugins live in Aidon home, never in the repo. A plugin
with a blocking scan finding is rejected even if the
owner would approve. A plugin with only warnings is
adopted only after approval, with warnings on the card.
"""
from tests.plugin_acceptance import passing_backend_factory

import json

from src.interface.approval import ApprovalGate, Decision
from src.agentic.loop import GoalRunner
from src.system.model_client import FakeClient
from src.capabilities.plugins import PluginRegistry, load_plugin, scan_plugin
from src.capabilities.sandbox import Sandbox


class _PassingSandbox:
    def run(self, command):
        return {"ok": True, "output": "pass"}


def _plugin_dir(tmp_path, name="upper", code=None, skills=None):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": skills or [],
        "acceptance_tests": [["python3", "-c", "from tool import upper; assert upper({'text':'aidon'}) == 'AIDON'"]]}), encoding="utf-8")
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


def test_skill_texts_only_trusted_standalone_skills(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path, skills=["skill.md"])
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True, test_backend_factory=passing_backend_factory)
    assert ok is True
    from src.capabilities.skill_library import Skill, SkillLibrary
    lib = SkillLibrary(home)
    lib.add(Skill(name="trusted-one", description="Trusted.",
                  test_commands=["python3 --version"]))
    lib.promote("trusted-one", lib.run_tests("trusted-one", _PassingSandbox()))
    lib.trust("trusted-one", approved=True)
    lib.add(Skill(name="validated-one", description="Staged.",
                  test_commands=["python3 --version"]))
    lib.promote("validated-one", lib.run_tests("validated-one", _PassingSandbox()))
    (home / "skills" / "validated-one.md").write_text(
        "# Staged draft\n", encoding="utf-8")
    (home / "skills" / "hand.md").write_text(
        "# Hand placed\n", encoding="utf-8")
    registry2 = PluginRegistry(home)
    registry2.load_all()
    texts = registry2.skill_texts()
    assert "upper/skill.md" in texts, "plugin skills are trusted by adoption"
    assert "trusted-one.md" in texts
    assert "validated-one.md" not in texts
    assert "hand.md" not in texts


def test_adopt_rejects_blocking_even_if_approved(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path, code=(
        "def upper(args):\n    return eval(args.get('text', ''))\n"))
    ok, findings, message = registry.adopt(
        directory, approve=lambda plugin, f: True, test_backend_factory=passing_backend_factory)
    assert ok is False
    assert not (home / "toolbox" / "upper").exists()


def test_adopt_copies_after_approval_and_registry_loads(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path, skills=["skill.md"])
    ok, findings, message = registry.adopt(
        directory, approve=lambda plugin, f: True, test_backend_factory=passing_backend_factory)
    assert ok is True
    assert (home / "toolbox" / "upper" / "plugin.json").exists()
    registry2 = PluginRegistry(home)
    registry2.load_all()
    assert registry2.call("upper", {"text": "hi"}) == "HI"
    assert "upper/skill.md" in registry2.skill_texts()


def test_adopt_without_approval_does_not_copy(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: False, test_backend_factory=passing_backend_factory)
    assert ok is False
    assert not (home / "toolbox" / "upper").exists()


def test_registry_adopt_enforces_capability_posture_before_approval(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    directory = _plugin_dir(tmp_path)
    manifest_path = directory / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["fs"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    approval_calls = []
    ok, _, message = registry.adopt(
        directory,
        approve=lambda plugin, findings: approval_calls.append(plugin.name),
        tool_posture="standard", test_backend_factory=passing_backend_factory)
    assert ok is False
    assert "capability or sandbox plugin" in message
    assert approval_calls == []
    assert not (home / "toolbox" / "upper").exists()


def test_loader_rejects_module_path_outside_plugin_tree(tmp_path):
    directory = _plugin_dir(tmp_path)
    manifest_path = directory / "plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["tools"][0]["module"] = "../outside.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    try:
        load_plugin(directory, execute=False)
    except ValueError as exc:
        assert "outside the plugin directory" in str(exc)
    else:
        raise AssertionError("escaping plugin module path was accepted")


def test_adoption_never_executes_plugin_code_before_approval(tmp_path):
    """Validation before the gate must be static.

    Importing a plugin module runs its top-level code. If that happens
    while building the approval card, an unreviewed plugin has already
    run on the host before the owner said yes. The marker file proves
    the module was not imported on the rejected path.
    """
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    marker = tmp_path / "executed.txt"
    code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('ran')\n"
        "def upper(args):\n    return str(args.get('text', '')).upper()\n")
    directory = _plugin_dir(tmp_path, code=code)
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: False, test_backend_factory=passing_backend_factory)
    assert ok is False
    assert not marker.exists()
    # After approval the copy is loaded for real and still works.
    ok, _, _ = registry.adopt(directory, approve=lambda plugin, f: True, test_backend_factory=passing_backend_factory)
    assert ok is True
    assert marker.exists()
    assert registry.call("upper", {"text": "hi"}) == "HI"


def test_loop_dispatches_to_plugin_tool(tmp_path):
    home = tmp_path / "home"
    registry = PluginRegistry(home)
    registry.adopt(_plugin_dir(tmp_path), approve=lambda plugin, f: True, test_backend_factory=passing_backend_factory)
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
