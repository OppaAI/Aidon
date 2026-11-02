"""Malformed model/checkpoint input and equivalent fallback memory ranking."""

import json
import random

import pytest

from src.agentic.checkpoint import RunCheckpoint
from src.agentic.plan_guard import validate_plan
from src.memory.stores import Point, InMemoryStore, SQLiteStore, _cosine, _rrf


@pytest.mark.parametrize('patch', [
    {"id": ["unhashable"]}, {"tool": {"unhashable": 1}},
    {"depends_on": 5}, {"depends_on": [["unhashable"]]},
    {"deliverable": ["unhashable"]}, {"deliverable": []}, {"args": []},
])
def test_plan_guard_reports_bad_types_instead_of_crashing(patch):
    step = {"id": "a", "tool": "shell", "args": {}}
    step.update(patch)
    assert validate_plan({"steps": [step]}, ["shell"])


def test_plan_guard_rejects_duplicate_ids_and_validates_long_dags_iteratively():
    step = {"id": "a", "tool": "shell", "args": {}}
    assert any("duplicate step id" in error for error in validate_plan({"steps": [step, step]}, ["shell"]))
    steps = [{"id": str(i), "tool": "shell", "args": {},
              "depends_on": [str(i - 1)] if i else []} for i in range(1500)]
    assert validate_plan({"steps": steps}, ["shell"]) == []
    steps[0]["depends_on"] = ["1499"]
    assert any("cycle" in error for error in validate_plan({"steps": steps}, ["shell"]))


@pytest.mark.parametrize('patch', [{"attempt": "oops"}, {"attempt": {}},
                                   {"passed": "not-a-list"}, {"verified": [123]}, {"phase": "corrupt"}])
def test_corrupt_checkpoint_fields_are_ignored(patch):
    assert RunCheckpoint.from_json(json.dumps({"run_id": "r", **patch})) is None


def test_memory_top_k_preserves_full_sort_ranking_and_ties(tmp_path):
    generator = random.Random(11)
    points = [Point(str(i), "memory", [generator.random(), generator.random()],
                    sparse=([1], [float(i % 4)])) for i in range(100)]
    vector, sparse = [0.3, 0.7], ([1], [3.0])
    for store in (InMemoryStore(), SQLiteStore(tmp_path)):
        for point in points:
            store.upsert("semantic", point)
        stored = list(store.iter_points("semantic"))
        dense = sorted(stored, key=lambda point: _cosine(vector, point.vector), reverse=True)
        keyword = sorted(stored, key=lambda point: min(3.0, point.sparse[1][0]), reverse=True)
        expected = [(hit.point.id, hit.score) for hit in _rrf(dense[:10], keyword[:10], 5)]
        actual = store.search("semantic", vector, "memory", limit=5, sparse=sparse)
        assert [(hit.point.id, hit.score) for hit in actual] == expected


def test_installed_prompts_find_the_packaged_manual_and_owner_profile(tmp_path, monkeypatch):
    from src.agentic import prompts
    import sys

    package = tmp_path / "venv/share/aidon"
    package.mkdir(parents=True)
    (package / "AGENTS.md").write_text("## Hard rules\nPackaged manual rule")
    owner = tmp_path / "home"
    owner.mkdir()
    (owner / "SOUL.md").write_text("Owner-defined persona")
    checkout = tmp_path / "empty"
    checkout.mkdir()
    monkeypatch.chdir(checkout)
    monkeypatch.setenv("AIDON_HOME", str(owner))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(prompts, "__file__", str(tmp_path / "venv/lib/src/agentic/prompts.py"))
    assert prompts._read_doc("AGENTS.md") == "## Hard rules\nPackaged manual rule"
    assert "Owner-defined persona" in prompts.build_system_prompt()


def _add_steer_notes(home, prefix):
    from src.interface.run_control import RunControl
    control = RunControl(home)
    for i in range(15):
        control.steer("run-1", f"{prefix}-{i}")
        control.set_stage("run-1", "sandbox")


def test_run_control_updates_from_multiple_processes_preserve_cancellation(tmp_path):
    import multiprocessing
    from src.interface.run_control import RunControl
    control = RunControl(tmp_path)
    control.register("run-1", "test")
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_add_steer_notes, args=(tmp_path, str(i))) for i in range(3)]
    for worker in workers:
        worker.start()
    control.cancel("run-1")
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    control.set_status("run-1", "done")
    assert control.is_cancelled("run-1")
    assert control.get("run-1")["status"] == "cancelled"
    assert len(set(control.steer_notes("run-1"))) == 45


def test_cancel_run_does_not_reject_another_runs_approval(tmp_path):
    import threading
    from src.interface.webui import WebUIGate
    gate = WebUIGate(timeout_seconds=0)
    gate.cards = {"a": {"verdict": None, "context": {"run_id": "one"}},
                  "b": {"verdict": None, "context": {"run_id": "two"}}}
    gate._events = {"a": threading.Event(), "b": threading.Event()}
    gate.cancel_run("one")
    assert gate.cards["a"]["verdict"] == "rejected"
    assert gate.cards["b"]["verdict"] is None


def test_corrupt_run_control_fails_closed(tmp_path):
    from src.interface.run_control import RunControl
    control = RunControl(tmp_path)
    control.register("run-1", "test")
    control._path("run-1").write_text("{corrupt")
    assert control.is_cancelled("run-1")
    assert not control.is_cancelled("unknown-run")


@pytest.mark.parametrize('result', [False, {"ok": False}])
def test_registry_outbox_keeps_notifications_when_sender_reports_failure(tmp_path, result):
    from src.capabilities.registry_db import Registry
    from src.agentic.lifecycle import NotificationOutbox
    registry = Registry(tmp_path)
    try:
        outbox = NotificationOutbox(registry)
        item_id = outbox.enqueue("test", "Notification")
        report = outbox.drain(lambda item: result)
        assert report["delivered"] == 0
        assert report["failed"] == [item_id]
        assert len(outbox.pending()) == 1
    finally:
        registry.close()


@pytest.mark.parametrize('host', ['127.attacker.example', '127.0.0.1.attacker.example', '127.not-an-ip'])
def test_loopback_host_guard_rejects_domains_starting_with_127(host):
    from src.interface.webui_auth import is_allowed_host, is_authorized
    assert not is_allowed_host(host + ':8765')
    assert not is_authorized({}, None, remote_addr=host)


def test_loopback_guard_accepts_real_loopback_addresses():
    from src.interface.webui_auth import is_allowed_host
    assert is_allowed_host('127.2.3.4:8765')
    assert is_allowed_host('[::1]:8765')
