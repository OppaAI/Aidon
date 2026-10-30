"""Capability Hub index: sources, dimensions, precedence,
health cache, search, and filters."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.capability_hub import CapabilityHub
from src.capabilities.capability_registry import CapabilityRegistry
from src.capabilities.skill_library import Skill, SkillLibrary

EVIDENCE = [{"passed": True, "test": "locked sandbox test"}]


def _approve(kind, title, body):
    return SimpleNamespace(verdict="approved")


def _plugin(tmp_path):
    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "plugin.json").write_text(json.dumps({
        "name": "demo", "version": "1",
        "description": "Demo plugin",
        "tools": [{"name": "demo_tool", "module": "demo_mod.py",
                   "function": "demo_fn",
                   "description": "Does a demo thing"}],
        "skills": ["demo_skill.md"]}))
    (plug / "demo_mod.py").write_text(
        "def demo_fn(args):\n    return 'ok'\n")
    (plug / "demo_skill.md").write_text("# Demo skill\nDo demos.\n")


@pytest.fixture
def home(tmp_path):
    _plugin(tmp_path)
    lib = SkillLibrary(tmp_path)
    lib.add(Skill(name="paper-search",
                  description="Search academic papers"))
    lib.promote("paper-search", {"passed": True})
    lib.trust("paper-search", approved=True)
    lib.add(Skill(name="draft-skill", description="Unfinished"))
    (tmp_path / "skills" / "hand.md").write_text(
        "# Hand placed\nNo lifecycle record.\n")
    (tmp_path / "mcp_servers.json").write_text(json.dumps({
        "papers": {"transport": "stdio", "command": "papers-mcp",
                   "tools": ["search_papers"]},
        "web": {"transport": "http", "url": "https://example.invalid/mcp",
                "tools": ["fetch_page"]},
        "broken": {}}))
    (tmp_path / "capability_images.json").write_text(json.dumps({
        "numpy-img": {"name": "numpy-img", "pip": ["numpy"],
                      "apt": [], "base": "ubuntu:24.04+py3.12" }}))
    reg = CapabilityRegistry(tmp_path)
    reg.register("papers", "mcp",
                 {"transport": "stdio", "command": "papers-mcp",
                  "tools": ["search_papers"]}, EVIDENCE)
    reg.register("numpy-img", "image", {}, EVIDENCE)
    reg.register("ghost-img", "image", {}, EVIDENCE)
    return tmp_path


def _by_id(hub):
    return {e["id"]: e for e in hub.index()}


def test_preloaded_registry_is_reused_without_reimport(tmp_path):
    plug = tmp_path / "plugins" / "demo"
    plug.mkdir(parents=True)
    (plug / "plugin.json").write_text(json.dumps({
        "name": "demo", "version": "1", "description": "Demo plugin",
        "tools": [{"name": "demo_tool", "module": "demo_mod.py",
                   "function": "demo_fn"}],
        "skills": []}))
    (plug / "demo_mod.py").write_text(
        "import os\n"
        "open(os.path.join(os.path.dirname(__file__), "
        "'imports.txt'), 'a').write('x')\n"
        "def demo_fn(args):\n    return 'ok'\n")
    from src.capabilities.plugins import PluginRegistry
    registry = PluginRegistry(tmp_path)
    registry.load_all()
    marker = plug / "imports.txt"
    assert marker.read_text() == "x"
    hub = CapabilityHub(tmp_path, plugins=registry)
    assert hub.get("plugin:demo") is not None
    assert marker.read_text() == "x", \
        "the hub must reuse the loaded registry, not import again"


def test_builtin_tools_present(home):
    entry = _by_id(CapabilityHub(home)).get("tool:write")
    assert entry["lifecycle"] == "builtin"
    assert entry["availability"] == "local"
    assert entry["verification"] == "passed"
    assert entry["provenance"] == "installed"


def test_plugin_and_its_skill(home):
    entries = _by_id(CapabilityHub(home))
    plugin = entries["plugin:demo"]
    assert plugin["lifecycle"] == "trusted"
    assert plugin["tools"] == ["demo_tool"]
    assert plugin["source"] == "plugins/demo"
    skill = entries["skill:demo/demo_skill.md"]
    assert skill["lifecycle"] == "trusted"
    assert skill["provenance"] == "operator"


def test_library_skill_trusted_and_synced_md_not_duplicated(home):
    entries = _by_id(CapabilityHub(home))
    skill = entries["skill:paper-search"]
    assert skill["lifecycle"] == "trusted"
    assert skill["source"] == "skills.json:paper-search"
    assert "skill:paper-search.md" not in entries, \
        "the synced discovery copy is not a second skill"


def test_hand_placed_md_is_an_unvetted_candidate(home):
    entry = _by_id(CapabilityHub(home))["skill:hand.md"]
    assert entry["lifecycle"] == "candidate"
    assert entry["verification"] == "untested"
    assert entry["provenance"] == "operator"


def test_operator_mcp_dimensions(home):
    entries = _by_id(CapabilityHub(home))
    web = entries["mcp:web"]
    assert web["lifecycle"] == "candidate"
    assert web["availability"] == "remote"
    assert web["permissions"] == ["network"]
    assert web["verification"] == "untested"
    broken = entries["mcp:broken"]
    assert broken["availability"] == "disconnected"


def test_registry_beats_operator_config(home):
    entry = _by_id(CapabilityHub(home))["mcp:papers"]
    assert entry["lifecycle"] == "validated"
    assert entry["provenance"] == "built"
    assert entry["source"] == "capabilities.json:papers"
    assert entry["tools"] == ["search_papers"]


def test_registry_image_with_and_without_manifest(home):
    entries = _by_id(CapabilityHub(home))
    assert entries["image:numpy-img"]["availability"] == "remote"
    assert entries["image:numpy-img"]["lifecycle"] == "validated"
    assert entries["image:ghost-img"]["availability"] == "disconnected"


def test_health_cache_merges_and_validates(home):
    hub = CapabilityHub(home)
    assert hub.get("mcp:web")["health"] == "unknown"
    hub.record_health("mcp:web", "healthy")
    entry = hub.get("mcp:web")
    assert entry["health"] == "healthy"
    assert entry["health_checked_at"]
    assert hub.get("mcp:web")["health"] == "healthy", "cache persists"
    with pytest.raises(ValueError, match="unknown health"):
        hub.record_health("mcp:web", "fine")
    with pytest.raises(ValueError, match="unknown capability"):
        hub.record_health("mcp:nope", "healthy")


def test_prune_health_removes_stale(home):
    hub = CapabilityHub(home)
    hub.record_health("mcp:web", "healthy")
    (home / "mcp_servers.json").write_text(json.dumps({}))
    assert hub.prune_health() == 1
    assert hub.prune_health() == 0


def test_search_spans_kinds_and_tools(home):
    hub = CapabilityHub(home)
    ids = {e["id"] for e in hub.search("paper")}
    assert "skill:paper-search" in ids
    assert "mcp:papers" in ids
    assert {e["id"] for e in hub.search("numpy")} == {"image:numpy-img"}
    assert hub.search("") == []


def test_filters_and_trusted(home):
    hub = CapabilityHub(home)
    assert {e["id"] for e in hub.list(kind="mcp")} == {
        "mcp:papers", "mcp:web", "mcp:broken"}
    trusted = {e["id"] for e in hub.trusted()}
    assert "tool:write" in trusted
    assert "plugin:demo" in trusted
    assert "skill:paper-search" in trusted
    assert "skill:draft-skill" not in trusted
    assert "mcp:papers" not in trusted, "validated is not trusted"
