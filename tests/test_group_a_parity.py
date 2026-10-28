"""Group A parity: skill library, learn cycle, scheduler depth, research cache."""

from datetime import datetime, timezone

import pytest

from src.agentic.learn_cycle import run_learn_cycle
from src.agentic.research_cache import ResearchCache, digest
from src.capabilities.skill_library import (
    Skill, SkillLibrary, check_code_sources, license_allows_reuse,
)
from src.interface.triggers import (
    is_oneshot, next_cron_run, parse_cron, parse_schedule,
)


class FakeSandbox:
    """run(cmd) -> {"ok", "output"}: ok unless the command says fail."""

    def __init__(self, failing=()):
        self.failing = set(failing)
        self.ran = []

    def run(self, cmd, timeout=60):
        self.ran.append(cmd)
        if cmd in self.failing:
            return {"ok": False, "output": "boom"}
        return {"ok": True, "output": "ok"}


def _skill(name, description="write a csv report", **kwargs):
    kwargs.setdefault("test_commands", ["python3 -m pytest tests/test_csv.py -q"])
    return Skill(name=name, description=description, **kwargs)


# ── license gate ──

def test_license_gate_mit_ok_gpl_and_unknown_blocked():
    assert license_allows_reuse("MIT")[0] is True
    assert license_allows_reuse("Apache-2.0")[0] is True
    assert license_allows_reuse("BSD-3-Clause")[0] is True
    assert license_allows_reuse("ISC")[0] is True
    assert license_allows_reuse("GPL-3.0")[0] is False
    assert license_allows_reuse("AGPL-3.0")[0] is False
    assert license_allows_reuse("Proprietary")[0] is False
    assert license_allows_reuse(None)[0] is False
    assert license_allows_reuse("")[0] is False
    check_code_sources([{"name": "x", "license": "MIT"}])
    with pytest.raises(PermissionError):
        check_code_sources([{"name": "y", "license": "GPL-3.0"}])
    with pytest.raises(PermissionError):
        check_code_sources([{"name": "z"}])


def test_library_add_refuses_non_permissive_source(tmp_path):
    library = SkillLibrary(tmp_path)
    with pytest.raises(PermissionError):
        library.add(_skill("bad", code_sources=[{"license": "GPL-2.0"}]))
    assert library.get("bad") is None


# ── library lifecycle ──

def test_promote_trust_retire_rollback_and_history(tmp_path):
    library = SkillLibrary(tmp_path)
    skill = library.add(_skill("csv_writer", provenance={
        "mission": "m1", "model": "test", "test_evidence": "tests pass",
        "supersedes": None}))
    assert skill.state == "candidate" and skill.version == 1
    assert len(library.version_history("csv_writer")) == 1

    library.promote("csv_writer", {"passed": True})
    assert library.get("csv_writer").state == "validated"
    with pytest.raises(PermissionError):
        library.trust("csv_writer", approved=False)
    library.trust("csv_writer", approved=True)
    assert library.get("csv_writer").state == "trusted"
    assert library.relevant("csv report") == [library.get("csv_writer")]

    library.retire("csv_writer", reason="superseded")
    retired = library.get("csv_writer")
    assert retired.state == "retired" and retired.retire_reason == "superseded"
    assert library.relevant("csv report") == []
    assert library.list(states=("retired",)) != []

    restored = library.rollback("csv_writer", 1)
    assert restored.state == "candidate" and restored.version == 1
    assert len(library.version_history("csv_writer")) >= 5


def test_promote_refuses_failing_report(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("x"))
    with pytest.raises(ValueError):
        library.promote("x", {"passed": False})
    assert library.get("x").state == "candidate"


def test_record_failure_auto_retires_at_max(tmp_path):
    library = SkillLibrary(tmp_path, max_failures=2)
    library.add(_skill("flaky"))
    library.record_failure("flaky")
    assert library.get("flaky").state == "candidate"
    library.record_failure("flaky")
    assert library.get("flaky").state == "retired"


def test_run_tests_pass_fail_and_trivial_rejection(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("good"))
    report = library.run_tests("good", FakeSandbox())
    assert report["passed"] is True

    library.add(_skill("bad", test_commands=["python3 -m pytest tests/test_bad.py -q"]))
    report = library.run_tests("bad", FakeSandbox(
        failing=["python3 -m pytest tests/test_bad.py -q"]))
    assert report["passed"] is False

    library.add(_skill("empty", test_commands=["true", "echo hello",
                                               'python3 -c "pass"']))
    report = library.run_tests("empty", FakeSandbox())
    assert report["passed"] is False
    assert all(result["trivial"] for result in report["results"])
    # A piped echo does real work and is not trivial.
    library.add(_skill("piped", test_commands=["echo '1,2' | python3 convert.py"]))
    report = library.run_tests("piped", FakeSandbox())
    assert report["passed"] is True


def test_run_tests_reports_missing_dependencies(tmp_path):
    """An unprovisioned skill is not a broken one."""
    library = SkillLibrary(tmp_path)
    library.add(_skill("needs_pkg", dependencies=["definitely_not_installed_pkg"]))
    # The import probe fails, so the report says which package is
    # missing and the skill is not marked passed.
    report = library.run_tests("needs_pkg", FakeSandbox(
        failing=['python3 -c "import definitely_not_installed_pkg"']))
    assert report["passed"] is False
    assert report["missing_dependencies"] == [
        {"kind": "python", "name": "definitely_not_installed_pkg"}]
    assert "install_python_package" in report["note"]

    # A verified install is provisioned, so the tests decide.
    library.add(_skill("installed", dependencies=[
        {"kind": "python", "name": "json", "verified": True}]))
    report = library.run_tests("installed", FakeSandbox())
    assert report["passed"] is True
    assert "missing_dependencies" not in report

    # Unverified and non-python dependencies are hints, not requirements.
    library.add(_skill("hints", dependencies=[
        {"kind": "python", "name": "claimed_pkg"},
        {"kind": "npm", "name": "left-pad", "verified": True}]))
    assert library.get("hints").required_packages() == []
    assert library.run_tests("hints", FakeSandbox())["passed"] is True


def test_relevant_trusted_first_then_validated(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("cand", description="write a csv report"))
    library.add(_skill("val", description="write a csv report"))
    library.promote("val", {"passed": True})
    library.add(_skill("trust", description="write a csv report"))
    library.trust("trust", approved=True)
    names = [skill.name for skill in library.relevant("csv report", limit=3)]
    assert names == ["trust", "val", "cand"]


# ── learn cycle ──

def test_learn_cycle_promotes_and_consolidates_validated(tmp_path):
    library = SkillLibrary(tmp_path)
    # Distinct description: this candidate must survive the
    # consolidation step, not be retired as a duplicate.
    library.add(_skill("cand_csv", description="convert json to yaml"))
    library.add(_skill("old_csv", description="write a csv report"))
    library.promote("old_csv", {"passed": True})
    library.add(_skill("new_csv", description="write a csv report"))
    library.promote("new_csv", {"passed": True})
    summary = run_learn_cycle(tmp_path, FakeSandbox())
    assert "cand_csv" in summary["promoted"]
    assert library.get("cand_csv").state == "validated"
    # Validated duplicates: older retired, newer kept.
    assert "old_csv -> new_csv" in summary["retired_duplicates"]
    assert library.get("old_csv").state == "retired"
    assert library.get("new_csv").state == "validated"


def test_learn_cycle_trusted_duplicates_only_reported(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("t1", description="write a csv report"))
    library.trust("t1", approved=True)
    library.add(_skill("t2", description="write a csv report"))
    library.trust("t2", approved=True)
    summary = run_learn_cycle(tmp_path, FakeSandbox())
    assert library.get("t1").state == "trusted"
    assert library.get("t2").state == "trusted"
    assert summary["trusted_duplicates_for_review"] == ["t1 ~ t2"]
    assert summary["retired_duplicates"] == []


def test_learn_cycle_degrades_without_sandbox(tmp_path):
    summary = run_learn_cycle(tmp_path, None)
    assert summary["promoted"] == []
    assert summary["skipped"] and "sandbox" in summary["skipped"][0]


def test_learn_cycle_skips_failing_candidate(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("broken"))
    summary = run_learn_cycle(
        tmp_path, FakeSandbox(failing=["python3 -m pytest tests/test_csv.py -q"]))
    assert summary["promoted"] == []
    assert library.get("broken").state == "candidate"
    assert any("broken" in item for item in summary["skipped"])


# ── scheduler depth ──

def test_cron_parse_next_run_and_timezone():
    cron = parse_cron("0 9 * * 1")
    assert cron["minute"] == {0} and cron["hour"] == {9} and cron["dow"] == {1}
    assert parse_cron("cron 0 9 * * *")["hour"] == {9}
    assert parse_cron("*/15 * * * *")["minute"] == {0, 15, 30, 45}
    assert parse_cron("0 9 * * 7")["dow"] == {0}
    with pytest.raises(ValueError):
        parse_cron("0 9 * *")
    with pytest.raises(ValueError):
        parse_cron("0 25 * * *")
    # 2026-10-26 is a Monday.
    monday = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    nxt = next_cron_run("0 9 * * *", monday)
    assert (nxt.day, nxt.hour) == (26, 9) and nxt.tzinfo is not None
    nxt = next_cron_run("0 9 * * *", datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc))
    assert nxt.day == 27
    # Wednesday 15:00 in Vancouver from Monday morning UTC.
    nxt = next_cron_run("0 15 * * 3", monday, tz="America/Vancouver")
    assert (nxt.day, nxt.hour) == (28, 15) and str(nxt.tzinfo) == "America/Vancouver"


def test_parse_schedule_and_oneshot():
    base = datetime(2026, 10, 26, 8, 0, tzinfo=timezone.utc)
    assert parse_schedule("every 2 hours", base) == datetime(
        2026, 10, 26, 10, 0, tzinfo=timezone.utc)
    assert parse_schedule("every 30 minutes", base) == datetime(
        2026, 10, 26, 8, 30, tzinfo=timezone.utc)
    assert parse_schedule("every 1 day", base) == datetime(
        2026, 10, 27, 8, 0, tzinfo=timezone.utc)
    at = parse_schedule("at:2026-11-04T18:00:00", tz="America/Vancouver")
    assert (at.day, at.hour) == (4, 18) and str(at.tzinfo) == "America/Vancouver"
    assert parse_schedule("0 9 * * *", base) == datetime(
        2026, 10, 26, 9, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_schedule("every 2 fortnights", base)
    assert is_oneshot("at:2026-11-04T18:00:00") is True
    assert is_oneshot("2026-11-04T18:00:00") is True
    assert is_oneshot("0 9 * * *") is False
    assert is_oneshot("every 30 minutes") is False


# ── research cache ──

def test_research_cache_store_fetch_search(tmp_path):
    cache = ResearchCache(tmp_path)
    assert cache.fetch("https://example.com/beef") is None
    cache.store("https://example.com/beef",
                "Roast beef is 12.99 per kg at the store.\nA second line.")
    assert cache.fetch("https://example.com/beef").startswith("Roast beef")
    cache.store("https://example.com/other", "Unrelated notes about cats.")
    hits = cache.search_research_cache("roast beef")
    assert len(hits) == 1 and hits[0]["url"] == "https://example.com/beef"
    assert any("Roast beef" in line for line in hits[0]["lines"])
    assert cache.search_research_cache("no such phrase") == []
    assert cache.search_research_cache("") == []
    # Storing the same URL replaces, it does not duplicate.
    cache.store("https://example.com/beef", "New text about roast beef.")
    assert cache.fetch("https://example.com/beef").startswith("New text")
    assert len(list((tmp_path / "research_cache").glob("*.txt"))) == 2
    assert digest("https://example.com/beef", "x") != digest("https://example.com/beef", "y")
