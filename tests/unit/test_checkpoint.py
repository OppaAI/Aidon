"""Unit tests for durable run checkpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agentic.checkpoint import (
    CheckpointStore,
    RunCheckpoint,
    deliverable_still_present,
    filter_resumable_passed,
    plan_to_dict,
    plan_fingerprint,
)
from src.capabilities.registry_db import Registry


def test_roundtrip_checkpoint(tmp_path: Path):
    reg = Registry(tmp_path)
    store = CheckpointStore(reg)
    cp = RunCheckpoint(
        run_id="run-1",
        goal="make a note",
        phase="sandbox",
        plan={"steps": [{"tool": "write", "deliverable": "a.md"}],
              "deliverables": ["a.md"], "criteria": []},
        passed=["a.md"],
        attempt=2,
    )
    store.save(cp)
    loaded = store.load("run-1")
    assert loaded is not None
    assert loaded.run_id == "run-1"
    assert loaded.phase == "sandbox"
    assert loaded.passed == ["a.md"]
    assert loaded.attempt == 2
    assert loaded.goal == "make a note"
    store.clear("run-1")
    assert store.load("run-1") is None or store.load("run-1").phase == "started"
    # clear writes empty string; from_json returns None
    assert RunCheckpoint.from_json(reg.get_state("run-1", "goal_checkpoint")) is None
    reg.close()


def test_corrupt_checkpoint_ignored(tmp_path: Path):
    reg = Registry(tmp_path)
    reg.save_state("run-x", "goal_checkpoint", "not-json{{{")
    store = CheckpointStore(reg)
    assert store.load("run-x") is None
    reg.close()


def test_filter_resumable_passed(tmp_path: Path):
    (tmp_path / "keep.md").write_text("ok", encoding="utf-8")
    kept = filter_resumable_passed(["keep.md", "gone.md", ""], tmp_path)
    assert "keep.md" in kept
    assert "gone.md" not in kept
    assert "" in kept
    assert deliverable_still_present(tmp_path, "keep.md") is True
    assert deliverable_still_present(tmp_path, "gone.md") is False


def test_plan_to_dict_from_object():
    class Step:
        tool = "write"
        args = {"file": "x.md", "text": "hi"}
        deliverable = "x.md"
        criteria = ""
        executor = "tool"

    class Plan:
        steps = [Step()]
        deliverables = ["x.md"]
        criteria = ["file exists"]

    data = plan_to_dict(Plan())
    assert data["deliverables"] == ["x.md"]
    assert data["steps"][0]["tool"] == "write"


def test_checkpoint_rejects_same_goal_with_different_plan(tmp_path: Path):
    reg = Registry(tmp_path)
    store = CheckpointStore(reg)
    first = {"steps": [{"tool": "write", "args": {"file": "a.md"},
                         "deliverable": "a.md"}],
             "deliverables": ["a.md"], "criteria": []}
    second = {"steps": [{"tool": "write", "args": {"file": "b.md"},
                          "deliverable": "b.md"}],
              "deliverables": ["b.md"], "criteria": []}
    store.save(RunCheckpoint(run_id="run-1", goal="make a note", plan=first,
                             passed=["a.md"]))
    assert store.load_for_goal("run-1", "make a note", plan=first) is not None
    assert store.load_for_goal("run-1", "make a note", plan=second) is None
    assert plan_fingerprint(first) != plan_fingerprint(second)
    reg.close()
