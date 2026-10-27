"""Mission snapshots, skill lifecycle, cross-file validation, code repair."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.capabilities.cross_validate import cross_validate
from src.capabilities.mission import (GuardViolation, MissionStore,
                                      grade_criteria, guard_external_action)
from src.capabilities.repair import apply_patch
from src.capabilities.skill_lifecycle import LifecycleStore
from src.capabilities.registry_db import Registry
from src.agentic.lifecycle import MissionLifecycle, NotificationOutbox


# Mission lifecycle -------------------------------------------------------

def test_mission_create_freeze_snapshot_and_status(tmp_path):
    store = MissionStore(tmp_path)
    spec = {"goal": "Dinner for two", "deliverables": ["menu.md", "ledger.json"]}
    mission = store.create("dinner", spec)
    assert mission["status"] == "draft"
    assert mission["deliverables"] == ["menu.md", "ledger.json"]
    snapshot = store.freeze("dinner")
    assert snapshot["status"] == "ready_for_review"
    assert snapshot["spec"] == spec
    assert snapshot["deliverables"] == ["menu.md", "ledger.json"]
    assert store.status("dinner") == "ready_for_review"
    snapshot_path = tmp_path / "missions" / "dinner" / "snapshot.json"
    assert snapshot_path.is_file()
    assert json.loads(snapshot_path.read_text()) == snapshot


def test_mission_freeze_hash_is_stable_and_covers_spec_only(tmp_path):
    store = MissionStore(tmp_path)
    spec = {"goal": "Dinner", "deliverables": ["menu.md"]}
    store.create("dinner", spec)
    first = store.freeze("dinner")
    second = store.freeze("dinner", force=True)
    # Timestamp may differ, hash must not: it covers spec + deliverables only.
    assert first["sha256"] == second["sha256"]
    expected = hashlib.sha256(
        json.dumps({"spec": spec, "deliverables": ["menu.md"]},
                   sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert first["sha256"] == expected


def test_mission_second_freeze_does_not_overwrite_without_force(tmp_path):
    store = MissionStore(tmp_path)
    store.create("dinner", {"goal": "One", "deliverables": []})
    first = store.freeze("dinner")
    # Change the mission spec after freezing, without force the snapshot stays.
    store.create("dinner", {"goal": "Two", "deliverables": []})
    again = store.freeze("dinner")
    assert again == first
    assert again["spec"]["goal"] == "One"
    forced = store.freeze("dinner", force=True)
    assert forced["spec"]["goal"] == "Two"


def test_mission_freeze_missing_raises_and_status_rules(tmp_path):
    store = MissionStore(tmp_path)
    try:
        store.freeze("missing")
        assert False, "freeze of a missing mission must raise"
    except FileNotFoundError:
        pass
    store.create("dinner", {"goal": "Dinner"})
    try:
        store.set_status("dinner", "bogus")
        assert False, "invalid status must raise"
    except ValueError:
        pass
    assert store.set_status("dinner", "done")["status"] == "done"
    assert store.unfreeze("dinner")["status"] == "in_progress"


def test_mission_freeze_requires_evidence_for_passed_criteria(tmp_path):
    store = MissionStore(tmp_path)
    store.create("dinner", {"goal": "Dinner", "deliverables": []})
    try:
        store.freeze("dinner", criteria=[{"name": "tests", "status": "passed"}])
        assert False, "passed criteria without evidence must fail"
    except ValueError:
        pass
    snapshot = store.freeze(
        "dinner", criteria=[{"name": "tests", "status": "passed",
                              "evidence": "pytest: 3 passed"}])
    assert snapshot["criteria"][0]["status"] == "passed"


def test_mission_external_actions_are_guarded():
    try:
        guard_external_action("testing", "git_push")
        assert False, "unapproved mission must not push"
    except GuardViolation:
        pass
    guard_external_action("approved", "git_push")


def test_registry_mission_lifecycle_and_durable_outbox(tmp_path):
    registry = Registry(tmp_path)
    try:
        lifecycle = MissionLifecycle(registry)
        mission = lifecycle.start("build it", "run-1", workspace=str(tmp_path / "draft"))
        lifecycle.transition(mission["id"], "testing")
        ref = lifecycle.freeze_snapshot(
            mission["id"], [], {"passed": True}, [],
            [{"name": "tests", "status": "passed", "evidence": "pytest passed"}],
        )
        assert lifecycle.verify_snapshot(mission["id"])
        assert lifecycle.get(mission["id"])["snapshot_ref"] == ref
        lifecycle.transition(mission["id"], "approved")
        assert lifecycle.review_record(mission["id"])["state"] == "approved"

        outbox = NotificationOutbox(registry, max_attempts=2)
        notification_id = outbox.enqueue("review", "Ready")
        assert outbox.pending()[0]["id"] == notification_id
        assert outbox.drain(lambda item: (_ for _ in ()).throw(RuntimeError("down")))["dead"] == []
        assert outbox.drain(lambda item: (_ for _ in ()).throw(RuntimeError("down")))["dead"] == [notification_id]
    finally:
        registry.close()


# Skill lifecycle and governor --------------------------------------------

def test_lifecycle_promotion_candidate_validated_trusted(tmp_path):
    store = LifecycleStore(tmp_path)
    assert store.state("search") == "candidate"
    assert store.record_success("search") == "validated"
    assert store.state("search") == "validated"
    for _ in range(3):
        store.record_success("search")
    assert store.state("search") == "validated"
    assert store.record_success("search") == "trusted"
    assert store.state("search") == "trusted"


def test_lifecycle_retirement_after_three_failures_is_sticky(tmp_path):
    store = LifecycleStore(tmp_path)
    assert store.record_failure("flaky") == "candidate"
    assert store.record_failure("flaky") == "candidate"
    assert store.record_failure("flaky") == "retired"
    assert store.state("flaky") == "retired"
    assert store.record_success("flaky") == "retired"
    assert store.governor_check("flaky") is False


def test_lifecycle_governor_daily_cap(tmp_path):
    store = LifecycleStore(tmp_path)
    assert store.governor_check("new-skill", max_learns_per_day=2) is True
    store.record_learn("new-skill")
    assert store.governor_check("new-skill", max_learns_per_day=2) is True
    store.record_learn("new-skill")
    assert store.governor_check("new-skill", max_learns_per_day=2) is False
    assert (tmp_path / "skill_lifecycle.json").is_file()


# Cross-file validation ---------------------------------------------------

def test_cross_validate_pass_and_case_insensitive(tmp_path):
    files = {"menu.md": "Roast Beef with potatoes",
             "ledger.json": "roast beef 1 kg, potatoes 2 kg"}
    result = cross_validate(files, [("menu.md", "ledger.json", ["roast beef", "potatoes"])])
    assert result["passed"] is True
    assert result["failed"] is False
    assert result["passed_count"] == 2
    assert all(detail["ok"] for detail in result["details"])


def test_cross_validate_fail_and_missing_file(tmp_path):
    files = {"menu.md": "Roast Beef, turkey"}
    result = cross_validate(files, [("menu.md", "ledger.json", ["roast beef", "turkey"])])
    assert result["passed"] is False
    assert result["failed"] is True
    assert result["failed_count"] == 2
    assert all(detail["ok"] is False for detail in result["details"])
    # Missing source file is also a failure, not a crash.
    result2 = cross_validate(files, [("missing.md", "ledger.json", ["x"])])
    assert result2["passed"] is False


# Code repair -------------------------------------------------------------

def test_repair_exact_match_and_backup(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello world\n")
    result = apply_patch(path, "world", "Aidon")
    assert result["ok"] is True
    assert result["backup_path"] == str(path) + ".bak"
    assert path.read_text() == "hello Aidon\n"
    assert Path(result["backup_path"]).read_text() == "hello world\n"


def test_repair_zero_or_multiple_matches_raise_and_leave_file(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("aaa bbb aaa\n")
    try:
        apply_patch(path, "missing", "x")
        assert False, "zero matches must raise"
    except ValueError:
        pass
    try:
        apply_patch(path, "aaa", "x")
        assert False, "multiple matches must raise"
    except ValueError:
        pass
    assert path.read_text() == "aaa bbb aaa\n"


def test_repair_rolls_back_broken_python(tmp_path):
    path = tmp_path / "script.py"
    path.write_text("x = 1\n")
    result = apply_patch(path, "x = 1", "def broken(:\n")
    assert result["ok"] is False
    assert result["error"]
    assert path.read_text() == "x = 1\n"


def test_repair_good_python_passes_compile(tmp_path):
    path = tmp_path / "script.py"
    path.write_text("x = 1\n")
    result = apply_patch(path, "x = 1", "x = 2", backup=False)
    assert result["ok"] is True
    assert result["backup_path"] is None
    assert path.read_text() == "x = 2\n"
