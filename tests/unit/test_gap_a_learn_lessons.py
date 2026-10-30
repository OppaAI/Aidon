"""Gap 2: learn cycle fail-closed + recorded learns.
Gap 6 (loop half): failure lessons recorded, repaired,
and recalled as planner context."""

from pathlib import Path

from src.agentic.learn_cycle import run_learn_cycle
from src.agentic.loop import GoalRunner, Plan, RunReport
from src.capabilities.skill_library import Skill, SkillLibrary
from src.capabilities.skill_lifecycle import LifecycleStore
from src.memory.failure_lessons import FailureLessons


class _FakeSandbox:
    def run(self, cmd, timeout=60):
        return {"ok": True, "output": "ok"}


def _skill(name):
    return Skill(name=name, description=f"skill {name}",
                 test_commands=["python3 -m pytest tests/test_x.py -q"])


def _allow(name, max_per_day=5):
    return True


def _runner(home):
    runner = GoalRunner.__new__(GoalRunner)
    runner.home = Path(home)
    runner._usage_totals = lambda: {"calls": 0, "total": 0, "in": 0,
                                    "out": 0, "think": 0}
    runner.network_available = False
    runner._adapted = False
    runner._adapted_from = ""
    runner._dag_summary = {}
    runner.memory = None
    runner.client = None
    runner.conversation_id = "test"
    runner._blocked = []
    runner._step_lines = []
    runner._elapsed_timer = None
    runner._replan_note = ""
    runner.run_id = ""
    return runner


def test_learn_cycle_without_governor_promotes_nothing(tmp_path):
    SkillLibrary(tmp_path).add(_skill("cand"))
    summary = run_learn_cycle(tmp_path, _FakeSandbox())
    assert summary["promoted"] == []
    assert any("governor" in item for item in summary["skipped"])
    assert SkillLibrary(tmp_path).get("cand").state == "candidate"


def test_learn_cycle_records_learns_and_governor_blocks(tmp_path):
    lib = SkillLibrary(tmp_path)
    lib.add(_skill("cand"))
    store = LifecycleStore(tmp_path)
    summary = run_learn_cycle(tmp_path, _FakeSandbox(),
                              governor_check=store.governor_check)
    assert summary["promoted"] == ["cand"]
    assert lib.get("cand").state == "validated"
    assert LifecycleStore(tmp_path)._data["cand"]["learns"]
    for _ in range(5):
        LifecycleStore(tmp_path).record_learn("blocked-skill")
    lib.add(_skill("blocked-skill"))
    summary = run_learn_cycle(tmp_path, _FakeSandbox(),
                              governor_check=LifecycleStore(tmp_path).governor_check)
    assert "blocked-skill" not in summary["promoted"]
    assert any("governor blocked: blocked-skill" in item
               for item in summary["skipped"])


def test_finalize_records_failure_lesson_and_repair(tmp_path):
    runner = _runner(tmp_path)
    plan = Plan(steps=[], deliverables=["out.md"], criteria=[])
    report = runner._finalize(RunReport(
        "failed", "make a video", plan, False, False, error="ffmpeg crashed"))
    assert report.status == "failed"
    lessons = FailureLessons(tmp_path).lessons_for("make a video")
    assert len(lessons) == 1
    assert lessons[0]["failure_class"] == "execution"
    assert lessons[0]["context"]["deliverables"] == ["out.md"]
    note = runner._failure_lessons_note("make a video")
    assert "execution" in note and "ffmpeg crashed" in note
    done = runner._finalize(RunReport(
        "done", "make a video", plan, True, True))
    assert done.status == "done"
    repaired = FailureLessons(tmp_path).lessons_for("make a video")
    assert repaired[0]["repair"] is not None
    assert "succeeded" in repaired[0]["repair"]["description"]


def test_finalize_skips_rejection_lessons(tmp_path):
    runner = _runner(tmp_path)
    plan = Plan(steps=[], deliverables=["out.md"], criteria=[])
    runner._finalize(RunReport(
        "failed", "make a video", plan, False, False,
        error="rejected by owner"))
    assert FailureLessons(tmp_path).lessons_for("make a video") == []


def test_failure_lessons_note_empty_without_lessons(tmp_path):
    assert _runner(tmp_path)._failure_lessons_note("nothing here") == ""
