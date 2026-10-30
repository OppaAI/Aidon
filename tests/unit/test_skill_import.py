"""Skill import + trust preservation: a Markdown file
becomes a library candidate, and trusting it must not
destroy the procedure text."""

import pytest

from src.capabilities.plugins import PluginRegistry
from src.capabilities.skill_library import (
    SkillLibrary, import_skill_file)


def _skill_file(tmp_path, name="video_edit.md", body=None):
    path = tmp_path / name
    path.write_text(body or (
        "# Video edit\n\n"
        "Edit video with ffmpeg: trim, crop, scale.\n\n"
        "## Trim\n\nffmpeg -ss ...\n"))
    return path


def test_import_creates_candidate_and_file(tmp_path):
    home = tmp_path / "home"
    skill = import_skill_file(home, _skill_file(tmp_path))
    assert skill.name == "video_edit" and skill.state == "candidate"
    assert skill.description.startswith("Edit video with ffmpeg")
    placed = home / "skills" / "video_edit.md"
    assert placed.is_file() and "Trim" in placed.read_text()
    assert SkillLibrary(home).get("video_edit") is not None


def test_import_sanitizes_name_and_refuses_reimport(tmp_path):
    home = tmp_path / "home"
    skill = import_skill_file(home, _skill_file(tmp_path, "My Skill.md"),
                              name="My Skill")
    assert skill.name == "My-Skill"
    assert (home / "skills" / "My-Skill.md").is_file()
    with pytest.raises(ValueError, match="already exists"):
        import_skill_file(home, _skill_file(tmp_path, "My Skill.md"),
                          name="My Skill")
    with pytest.raises(ValueError, match="not found"):
        import_skill_file(home, tmp_path / "missing.md")


def test_trust_preserves_procedure_and_planner_sees_it(tmp_path):
    home = tmp_path / "home"
    import_skill_file(home, _skill_file(tmp_path))
    lib = SkillLibrary(home)
    lib.promote("video_edit", {"passed": True, "evidence": "test"})
    lib.trust("video_edit", approved=True)
    placed = home / "skills" / "video_edit.md"
    assert "Trim" in placed.read_text(), \
        "trust must not overwrite procedure text with metadata"
    texts = PluginRegistry(home).skill_texts()
    assert "video_edit.md" in texts and "Trim" in texts["video_edit.md"]


def test_trust_without_file_writes_metadata_stub(tmp_path):
    home = tmp_path / "home"
    lib = SkillLibrary(home)
    from src.capabilities.skill_library import Skill
    lib.add(Skill(name="stub-skill", description="A stub"))
    lib.promote("stub-skill", {"passed": True, "evidence": "test"})
    lib.trust("stub-skill", approved=True)
    stub = home / "skills" / "stub-skill.md"
    assert stub.is_file() and "A stub" in stub.read_text()
