"""Skill ## Tests section: test commands live in the
skill file, import reads them, to_markdown writes them,
and only then can the harvest promote an imported skill."""

from src.capabilities.skill_library import (
    Skill,
    SkillLibrary,
    import_skill_file,
    parse_test_commands,
)


class _FakeSandbox:
    def run(self, cmd, timeout=60):
        return {"ok": cmd != "false", "output": "ok"}


MD = """# md-table-to-csv

Convert a Markdown table to CSV with python3.

## Tests

```sh
python3 --version
```

## Notes

Not a test: this line is outside the Tests section.
"""


def test_parse_tests_section(tmp_path):
    assert parse_test_commands(MD) == ["python3 --version"]
    assert parse_test_commands("# No tests here\ntext") == []
    assert parse_test_commands("## Tests\n\n# comment\n\n") == []


def test_import_reads_test_commands(tmp_path):
    source = tmp_path / "draft.md"
    source.write_text(MD, encoding="utf-8")
    skill = import_skill_file(tmp_path, source, name="md-table-to-csv")
    assert skill.state == "candidate"
    assert skill.test_commands == ["python3 --version"]
    stored = SkillLibrary(tmp_path).get("md-table-to-csv")
    assert stored.test_commands == ["python3 --version"]


def test_to_markdown_round_trips_tests():
    skill = Skill(name="x", description="d",
                  test_commands=["python3 --version"])
    assert parse_test_commands(skill.to_markdown()) == \
        ["python3 --version"]


def test_harvest_promotes_imported_skill(tmp_path):
    source = tmp_path / "draft.md"
    source.write_text(MD, encoding="utf-8")
    import_skill_file(tmp_path, source, name="md-table-to-csv")
    library = SkillLibrary(tmp_path)
    promoted = library.harvest_candidates(_FakeSandbox())
    assert promoted == ["md-table-to-csv"]
    assert library.get("md-table-to-csv").state == "validated"


def test_import_without_tests_never_promotes(tmp_path):
    source = tmp_path / "draft.md"
    source.write_text("# plain\nJust a procedure.\n", encoding="utf-8")
    import_skill_file(tmp_path, source, name="plain")
    library = SkillLibrary(tmp_path)
    assert library.harvest_candidates(_FakeSandbox()) == []
    assert library.get("plain").state == "candidate"
