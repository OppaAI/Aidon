"""Tests for the loop-behaviour modules."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from src.agentic.artifact_inventory import (
    goal_files, inventory, inventory_markdown)
from src.agentic.delivery_summary import delivery_summary, problem_narration
from src.agentic.file_versions import (
    current_hash, list_versions, read_version, write_versioned)
from src.agentic.image_guard import (
    allowed_tiers, guard_image_input, image_allowed)
from src.agentic.plan_guard import (
    guard_plan, plan_first_summary, validate_plan)
from src.agentic.resume import (
    list_runs, load_run_state, resume_state, save_run_state)
from src.agentic.step_lines import (
    StepLine, record_attempt, render_step_lines)
from src.agentic.think_effort import (
    EFFORT_LEVELS, effort_for, resolve_effort, sampling_for)


def _good_plan():
    return {"goal": "write a report", "steps": [
        {"id": "s1", "tool": "write", "args": {"file": "a.md"},
         "deliverable": "a.md", "depends_on": []},
        {"id": "s2", "tool": "read", "args": {"file": "a.md"},
         "deliverable": "check", "depends_on": ["s1"]},
    ]}


# --- resume ---
def test_resume_round_trip_and_list(tmp_path):
    state = {"goal": "do it", "steps": ["s1"], "completed": []}
    save_run_state(tmp_path, "run1", state)
    assert load_run_state(tmp_path, "run1") == state
    assert list_runs(tmp_path) == ["run1"]
    ok, loaded, error = resume_state(tmp_path, "run1")
    assert ok and loaded == state and error == ""


def test_resume_invalid_state(tmp_path):
    save_run_state(tmp_path, "bad", {"goal": "x", "steps": [], "completed": "no"})
    ok, state, error = resume_state(tmp_path, "bad")
    assert not ok and state is None and "completed" in error
    ok, _, _ = resume_state(tmp_path, "missing")
    assert not ok
    assert load_run_state(tmp_path, "missing") is None


# --- plan guard ---
def test_plan_guard_valid_and_summary():
    plan = _good_plan()
    assert validate_plan(plan, ["write", "read"]) == []
    assert guard_plan(plan, ["write", "read"]) is plan
    summary = plan_first_summary(plan)
    assert "write a report" in summary and "2 step" in summary


def test_plan_guard_each_failure_kind():
    plan = {"steps": [
        {"id": "a", "tool": "nope", "args": {}, "deliverable": "x"},
        {"id": "b", "tool": "write", "deliverable": "x",
         "depends_on": ["zzz"]},
        {"id": "c", "tool": "write", "args": {}, "deliverable": "x",
         "depends_on": ["c"]},
    ]}
    failures = validate_plan(plan, ["write"])
    assert any("unknown tool" in f for f in failures)
    assert any("no args" in f for f in failures)
    assert any("duplicate deliverable" in f for f in failures)
    assert any("unknown step" in f for f in failures)
    assert any("depends on itself" in f for f in failures)
    with pytest.raises(ValueError):
        guard_plan(plan, ["write"])


def test_plan_guard_cycle():
    plan = {"steps": [
        {"id": "a", "tool": "write", "args": {}, "deliverable": "x",
         "depends_on": ["b"]},
        {"id": "b", "tool": "write", "args": {}, "deliverable": "y",
         "depends_on": ["a"]},
    ]}
    assert any("cycle" in f for f in validate_plan(plan, ["write"]))


# --- think effort ---
def test_think_effort_table_and_escalation():
    assert EFFORT_LEVELS == ("off", "low", "medium", "high")
    assert effort_for("verify") == "low"
    assert effort_for("research") == "medium"
    assert effort_for("unknown-phase") == "low"
    assert effort_for("verify", {"verify": "high"}) == "high"
    assert sampling_for("verify")["temperature"] == 0.0
    assert sampling_for("respond")["temperature"] > sampling_for("verify")["temperature"]
    assert resolve_effort("verify", escalate=True) == "medium"
    assert resolve_effort("verify", requested="high", escalate=True) == "high"
    with pytest.raises(ValueError):
        resolve_effort("verify", requested="extreme")


# --- file versions ---
def test_versioned_writes_and_conflict(tmp_path):
    assert write_versioned(tmp_path, "a.txt", "one") == 1
    assert write_versioned(tmp_path, "a.txt", "one") == 1
    assert write_versioned(tmp_path, "a.txt", "two") == 2
    assert read_version(tmp_path, "a.txt") == "two"
    assert read_version(tmp_path, "a.txt", 1) == "one"
    assert list_versions(tmp_path, "a.txt") == [1, 2]
    seen = current_hash(tmp_path, "a.txt")
    (tmp_path / "a.txt").write_text("changed elsewhere", encoding="utf-8")
    result = write_versioned(tmp_path, "a.txt", "three", expected_hash=seen)
    assert result["conflict"] is True
    assert result["existing"] == "changed elsewhere"
    assert result["incoming"] == "three"
    assert read_version(tmp_path, "a.txt") == "changed elsewhere"


# --- artifact inventory ---
def test_inventory_goal_files_markdown(tmp_path):
    (tmp_path / "roast-beef-menu.md").write_text("roast beef dinner", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated", encoding="utf-8")
    rows = inventory(tmp_path)
    assert {r["path"] for r in rows} == {"roast-beef-menu.md", "notes.txt"}
    assert rows[0]["size"] > 0 and rows[0]["kind"] in ("text", "file")
    matched = goal_files(tmp_path, "plan a roast beef dinner")
    assert [r["path"] for r in matched] == ["roast-beef-menu.md"]
    md = inventory_markdown(tmp_path)
    assert "| path | size | kind |" in md and "roast-beef-menu.md" in md


# --- delivery summary ---
def test_delivery_summary_and_narration():
    report = {"goal": "make dinner menu",
              "deliverables": [{"name": "menu.pdf", "status": "pass"},
                               {"name": "list", "status": "fail"}],
              "files": ["menu.pdf"], "elapsed": "12s", "repairs": 2}
    md = delivery_summary(report)
    assert "make dinner menu" in md and "menu.pdf: pass" in md
    assert "list: fail" in md and "Repairs:** 2" in md
    text = problem_narration("store site is down",
                             {"blocked_on": "price check",
                              "tried": ["retry", "cached prices"],
                              "next_step": "try again later"})
    assert "store site is down" in text and "cached prices" in text
    assert "try again later" in text


# --- image guard ---
def test_image_guard_tiers():
    assert allowed_tiers() == {"strong"}
    assert image_allowed("fast", False) is True
    assert image_allowed("fast", True) is False
    assert image_allowed("strong", True) is True
    ok, reason = guard_image_input("fast", ["a.png"])
    assert not ok and "text-only" in reason
    assert guard_image_input("strong", ["a.png"])[0] is True
    assert guard_image_input("fast", [])[0] is True


# --- step lines ---
def test_step_lines_rendering_with_elapsed():
    lines = []
    record_attempt(lines, "s1", "write", "ok", "wrote a.md")
    assert len(lines) == 1 and lines[0].status == "ok"
    manual = StepLine(step_id="s2", tool="shell", status="fail",
                      detail="exit 1",
                      started_at="2026-10-26T00:00:00+00:00",
                      ended_at="2026-10-26T00:00:02+00:00")
    assert manual.elapsed_seconds() == 2.0
    rendered = render_step_lines(lines + [manual])
    assert "s1 write ok" in rendered
    assert "s2 shell fail 2.0s exit 1" in rendered
