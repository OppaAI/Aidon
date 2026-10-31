"""Capability triage: inventory assembly and confidence rules."""

import json
from types import SimpleNamespace

from src.agentic.capability_triage import (
    build_inventory, inventory_text, triage)


class _StubClef:
    def __init__(self, choice, confidence):
        self.choice = choice
        self.confidence = confidence
        self.seen = []

    def decide_choice(self, state, name, instructions, criteria):
        self.seen.append((state, name))
        assert self.choice in criteria
        return self.choice, self.confidence, {}


def _inventory():
    return {"tools": ["write", "read", "shell"],
            "skills": [{"name": "pdf", "description": "make pdfs"}],
            "mcp_servers": ["notion"]}


def test_inventory_reads_tools_skills_and_mcp_file(tmp_path):
    (tmp_path / "mcp_servers.json").write_text(
        json.dumps({"notion": {}, "todoist": {}}))
    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "plugin.json").write_text(json.dumps({
        "name": "demo", "version": "1", "description": "Demo plugin",
        "tools": [{"name": "edit_image", "module": "demo_mod.py",
                   "function": "demo_fn"}],
        "skills": ["pdf.md"]}))
    (plug / "demo_mod.py").write_text(
        "def demo_fn(args):\n    return 'ok'\n")
    (plug / "pdf.md").write_text("# PDF skill\nmake pdfs with text\n")
    from src.capabilities.plugins import PluginRegistry
    registry = PluginRegistry(tmp_path)
    registry.load_all()
    tools = SimpleNamespace(names=lambda: ["write", "read"])
    inv = build_inventory(tmp_path, tools, registry, {"tts_speak": None})
    assert inv["tools"] == ["edit_image", "read", "tts_speak", "write"]
    assert inv["skills"] == [{"name": "demo/pdf.md",
                              "description": "Skill bundled with plugin demo"}]
    assert inv["mcp_servers"] == ["notion", "todoist"]
    text = inventory_text(inv)
    assert "edit_image" in text and "notion" in text


def test_inventory_skills_are_trusted_only(tmp_path):
    from src.capabilities.skill_library import Skill, SkillLibrary
    lib = SkillLibrary(tmp_path)
    lib.add(Skill(name="trusted-skill", description="Trusted."))
    lib.trust("trusted-skill", approved=True)
    lib.add(Skill(name="validated-skill", description="Staged."))
    lib.promote("validated-skill", {"passed": True})
    lib.add(Skill(name="candidate-skill", description="Draft."))
    (tmp_path / "skills" / "hand.md").write_text("# Hand\n")
    tools = SimpleNamespace(names=lambda: ["write"])
    inv = build_inventory(tmp_path, tools, None, None)
    assert [s["name"] for s in inv["skills"]] == ["trusted-skill"]


def test_inventory_sees_registry_mcp_and_filters_dead_entries(tmp_path):
    from src.capabilities.capability_hub import CapabilityHub
    from src.capabilities.capability_registry import CapabilityRegistry
    (tmp_path / "mcp_servers.json").write_text(json.dumps({
        "down-server": {"transport": "stdio", "command": "down-mcp"}}))
    reg = CapabilityRegistry(tmp_path)
    evidence = [{"passed": True, "test": "connect and list tools"}]
    reg.register("reg-server", "mcp",
                 {"transport": "http", "url": "https://example.invalid/mcp",
                  "tools": ["fetch_page"]}, evidence)
    reg.register("old-server", "mcp",
                 {"transport": "stdio", "command": "old-mcp",
                  "tools": ["old_tool"]}, evidence)
    reg.retire("old-server", "replaced by reg-server")
    CapabilityHub(tmp_path).record_health("mcp:down-server", "unavailable")
    tools = SimpleNamespace(names=lambda: ["write"])
    inv = build_inventory(tmp_path, tools, None, None)
    assert inv["mcp_servers"] == ["reg-server"]


def test_inventory_tolerates_missing_sources(tmp_path):
    inv = build_inventory(tmp_path, None, None, None)
    assert inv == {"tools": [], "skills": [], "mcp_servers": []}


def test_triage_covered_and_need_states():
    for status in ("covered", "need_skill", "need_mcp", "need_plugin"):
        result = triage("Do a thing", _inventory(),
                        _StubClef(status, 0.9))
        assert result["status"] == status
        assert result["confidence"] == 0.9
        if status != "covered":
            assert result["note"], "non-covered triage must carry a note"


def test_triage_low_confidence_proceeds_as_covered():
    result = triage("Do a thing", _inventory(), _StubClef("blocked", 0.4))
    assert result["status"] == "covered"
    assert result["note"] == ""


def test_triage_uncertain_block_downgrades_to_plugin():
    result = triage("Do a thing", _inventory(), _StubClef("blocked", 0.7))
    assert result["status"] == "need_plugin"
    assert "plugin" in result["note"]


def test_triage_high_confidence_block_stops():
    result = triage("Do a thing", _inventory(), _StubClef("blocked", 0.95))
    assert result["status"] == "blocked"
    assert "stopping before planning" in result["note"]


def test_triage_without_client_is_covered():
    result = triage("Do a thing", _inventory(), None)
    assert result["status"] == "covered"


def test_triage_state_carries_goal_and_inventory():
    stub = _StubClef("need_mcp", 0.9)
    triage("Connect Notion", _inventory(), stub)
    state, name = stub.seen[0]
    assert state["goal"] == "Connect Notion"
    assert "write" in state["inventory"] and "notion" in state["inventory"]
