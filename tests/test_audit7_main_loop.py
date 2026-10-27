"""Seventh-audit fixes, part A: steer/cancel registration, initial-plan
budget failures, dag-run edges, guidance, kernel notice, trace
attribution.

main.py branch wiring (register + print) is covered by the dag-run
tests below, which drive main() end to end; the loop-side contract
(run_id + run_control actually gating a run) is covered by the
cancel test.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, "src")

from src.agentic.budgets import Budget
from src.agentic.loop import GoalRunner
from src.agentic.trace import TraceRecorder
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.run_control import RunControl
from src.system.model_client import FakeClient

PLAN_JSON = json.dumps({
    "steps": [{"tool": "write", "args": {"file": "a.txt", "text": "hi"},
               "deliverable": "a.txt"}],
    "deliverables": ["a.txt"],
    "criteria": ["a.txt exists"],
})


def _runner(tmp_path, client=None, **kwargs):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(client or FakeClient(PLAN_JSON),
                      Sandbox(tmp_path / "work", 120), gate, tmp_path,
                      **kwargs)


def test_fresh_plan_budget_breach_fails_run_not_crash(tmp_path):
    runner = _runner(tmp_path, budget=Budget(max_tokens=0, max_steps=50,
                                             max_seconds=3600))
    report = runner.run("write a.txt")
    assert report.status == "failed"
    assert "budget exceeded" in (report.error or "")


def test_spec_plan_budget_breach_fails_run_not_crash(tmp_path):
    runner = _runner(tmp_path, budget=Budget(max_tokens=0, max_steps=50,
                                             max_seconds=3600))
    report = runner.run("write x.txt", spec={"deliverables": ["x.txt"]})
    assert report.status == "failed"
    assert "budget exceeded" in (report.error or "")
    assert report.plan.deliverables == ["x.txt"]


def test_guidance_reaches_plan_prompt(tmp_path):
    client = FakeClient(PLAN_JSON)
    runner = _runner(tmp_path, client=client, guidance="Always paint it blue")
    plan = runner._plan("write a report")
    assert plan.deliverables == ["a.txt"]
    user_message = client.calls[0][1].content
    assert "Always paint it blue" in user_message
    assert "binding" in user_message


def test_guidance_empty_adds_nothing(tmp_path):
    client = FakeClient(PLAN_JSON)
    runner = _runner(tmp_path, client=client)
    runner._plan("write a report")
    assert "binding" not in client.calls[0][1].content


def test_kernel_notice_on_stderr_in_kernel_mode(tmp_path, capsys):
    runner = _runner(tmp_path, kernel_mode=True,
                     budget=Budget(max_tokens=0, max_steps=50,
                                   max_seconds=3600))
    runner.run("write a.txt")
    assert "Kernel mode on: only seed tools" in capsys.readouterr().err


def test_no_kernel_notice_outside_kernel_mode(tmp_path, capsys):
    runner = _runner(tmp_path, kernel_mode=False,
                     budget=Budget(max_tokens=0, max_steps=50,
                                   max_seconds=3600))
    runner.run("write a.txt")
    assert "Kernel mode on" not in capsys.readouterr().err


def test_cancelled_registered_run_reports_cancelled(tmp_path):
    control = RunControl(tmp_path)
    assert control.register("run-1", "write a.txt", "default")
    assert control.cancel("run-1") is True
    runner = _runner(tmp_path, run_id="run-1", run_control=control)
    report = runner.run("write a.txt")
    assert report.status == "cancelled"


def test_trace_attribution_shared_recorder(tmp_path):
    recorder = TraceRecorder(tmp_path, project_id="default")
    parent = _runner(tmp_path, trace=recorder)
    sub = _runner(tmp_path, trace=recorder, run_id="sub-abc")
    parent._trace("sandbox", "started", "parent step")
    sub._trace("sandbox", "started", "sub step")
    parent_file = recorder.trace_path.read_text()
    sub_file = (tmp_path / "trace" / "sub-abc.jsonl").read_text()
    assert "parent step" in parent_file
    assert "sub step" not in parent_file
    assert "sub step" in sub_file
    assert "parent step" not in sub_file


def test_trace_no_run_id_falls_back_to_recorder(tmp_path):
    recorder = TraceRecorder(tmp_path, project_id="default")
    runner = _runner(tmp_path, trace=recorder)
    assert runner.run_id is None
    runner._trace("sandbox", "started", "fallback step")
    assert "fallback step" in recorder.trace_path.read_text()


def _dag_home(tmp_path, monkeypatch, nodes):
    from src.agentic.dag_store import DagStore
    home = tmp_path / "home"
    monkeypatch.setenv("AIDON_HOME", str(home))
    monkeypatch.chdir(tmp_path)
    store = DagStore(home)
    store.save("d1", "test dag", nodes)
    return home


def _write_node(node_id, file, text="hi"):
    return {"id": node_id, "tool": "write", "args": {"file": file, "text": text},
            "deliverable": file, "depends_on": [], "executor": "tool"}


def test_dag_run_via_main(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch, [_write_node("n1", "hello.txt")])
    assert main(["dag", "run", "d1"]) == 0
    assert (home / "workspace" / "hello.txt").read_text() == "hi"


def test_dag_run_absolute_workdir_path_relativized(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch, [])
    workdir = home / "workspace"
    workdir.mkdir(parents=True, exist_ok=True)
    from src.agentic.dag_store import DagStore
    DagStore(home).save("d1", "test dag",
                        [_write_node("n1", str(workdir / "abs.txt"))])
    assert main(["dag", "run", "d1"]) == 0
    assert (workdir / "abs.txt").read_text() == "hi"


def test_dag_run_duplicate_ids_refused(tmp_path, monkeypatch):
    from src.system.main import main
    home = _dag_home(tmp_path, monkeypatch,
                     [_write_node("n1", "a.txt"),
                      _write_node("n1", "b.txt")])
    assert main(["dag", "run", "d1"]) == 2
    assert not (home / "workspace" / "a.txt").exists()
    assert not (home / "workspace" / "b.txt").exists()


def test_dag_relativize_unit(tmp_path):
    from src.agentic.dag import DAG, DAGNode
    workdir = str(tmp_path / "work")
    dag = DAG([DAGNode(id="n1", tool="write",
                       args={"file": f"{workdir}/hello.txt"},
                       deliverable="hello.txt")])
    assert dag.relativize(workdir).nodes["n1"].args["file"] == "hello.txt"
