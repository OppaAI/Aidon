"""Mission skill harvest: ordinary runs test and promote
candidate skills, bounded by the run's skill budget.
Trust still needs owner approval."""

import json
from pathlib import Path

from src.agentic.loop import GoalRunner, Plan, RunReport
from src.capabilities.acquisition_governor import AcquisitionGovernor
from src.capabilities.sandbox import Sandbox
from src.capabilities.skill_library import Skill, SkillLibrary


class _FakeSandbox:
    def __init__(self, ok=True):
        self.ok = ok

    def run(self, cmd, timeout=60):
        # "false" fails; every other command honors the fake.
        return {"ok": self.ok and cmd != "false", "output": "ok"}


def _skill(name, commands):
    return Skill(name=name, description=f"skill {name}",
                 test_commands=commands)


def _runner(home, sandbox=None, governor=None):
    runner = GoalRunner.__new__(GoalRunner)
    runner.home = Path(home)
    runner.run_id = "run-1"
    runner.conversation_id = "c1"
    runner.sandbox = sandbox
    runner.acquisition_governor = governor
    runner._elapsed_timer = None
    runner._blocked = []
    runner.network_available = True
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner._replan_note = ""
    runner._step_lines = []
    runner.memory = None
    runner._mission_lifecycle = None
    runner._mission = None
    runner.client = None
    runner._counting_clients = lambda: []
    return runner


def _done_report():
    return RunReport(status="done", goal="do the thing",
                     plan=Plan(), verified=True, approved=True)


def test_harvest_promotes_passing_candidate(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("echoer", ["python3 --version"]))
    promoted = library.harvest_candidates(_FakeSandbox(ok=True))
    assert promoted == ["echoer"]
    assert library.get("echoer").state == "validated"


def test_harvest_skips_failing_and_untested_candidates(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("broken", ["false"]))
    library.add(_skill("untested", []))
    promoted = library.harvest_candidates(_FakeSandbox(ok=True))
    assert promoted == []
    assert library.get("broken").state == "candidate"
    assert library.get("untested").state == "candidate"


def test_harvest_stops_at_skill_budget(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("first", ["python3 --version"]))
    library.add(_skill("second", ["python3 --version"]))
    governor = AcquisitionGovernor(max_skills=1)
    promoted = library.harvest_candidates(_FakeSandbox(ok=True),
                                          governor=governor)
    assert promoted == ["first"]
    assert library.get("second").state == "candidate"


def test_harvest_with_zero_budget_promotes_nothing(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("echoer", ["python3 --version"]))
    governor = AcquisitionGovernor(max_skills=0)
    promoted = library.harvest_candidates(_FakeSandbox(ok=True),
                                          governor=governor)
    assert promoted == []
    assert library.get("echoer").state == "candidate"


def test_real_sandbox_runs_string_test_commands(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("echoer", ["python3 --version"]))
    sandbox = Sandbox(tmp_path / "work")
    report = library.run_tests("echoer", sandbox)
    assert report["passed"] is True


def test_finalize_done_harvests_and_reports(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("echoer", ["python3 --version"]))
    governor = AcquisitionGovernor(max_skills=2)
    runner = _runner(tmp_path, sandbox=_FakeSandbox(ok=True),
                     governor=governor)
    report = runner._finalize(_done_report())
    assert report.promoted_skills == ["echoer"]
    assert library.get("echoer").state == "validated"


def test_finalize_done_with_no_budget_promotes_nothing(tmp_path):
    library = SkillLibrary(tmp_path)
    library.add(_skill("echoer", ["python3 --version"]))
    governor = AcquisitionGovernor(max_skills=0)
    runner = _runner(tmp_path, sandbox=_FakeSandbox(ok=True),
                     governor=governor)
    report = runner._finalize(_done_report())
    assert getattr(report, "promoted_skills", None) in (None, [])
    assert library.get("echoer").state == "candidate"


def test_finalize_failed_records_diagnostic_error(tmp_path):
    runner = _runner(tmp_path, sandbox=_FakeSandbox(ok=True))
    failed = RunReport(status="failed", goal="do the thing",
                       plan=Plan(), verified=False, approved=False,
                       error="boom")
    runner._finalize(failed)
    errors = (tmp_path / "logs" / "errors.jsonl").read_text(
        encoding="utf-8")
    record = json.loads(errors.strip().splitlines()[-1])
    assert record["run_id"] == "run-1"
    assert "boom" in record["message"]
