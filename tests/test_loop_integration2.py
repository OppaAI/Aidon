"""Loop integrations: plan-first research + replan, proven-run DAG learn."""

import json

from src.agentic.loop import GoalRunner, Plan, RunReport
from src.agentic.dag_store import DagStore
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision


def _runner(tmp_path, researcher=None, client=None):
    return GoalRunner(client or FakeClient(""),
                      Sandbox(tmp_path / "work"),
                      ApprovalGate(lambda kind, title, body: Decision("approved", "test")),
                      tmp_path, researcher=researcher)


def _plan(**kw):
    base = {"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"},
                       "deliverable": "a.md"}],
            "deliverables": ["a.md"], "criteria": []}
    base.update(kw)
    return Plan(**base)


# ---- plan-first research + replan -------------------------------------------

def test_empty_needs_research_never_calls_researcher(tmp_path):
    calls = []
    runner = _runner(tmp_path, researcher=lambda item: calls.append(item) or [])
    plan = _plan(needs_research=[])
    out = runner._research_and_replan(plan, "write a.md")
    assert out is plan
    assert calls == []


def test_no_researcher_keeps_original_plan(tmp_path):
    runner = _runner(tmp_path, researcher=None)
    plan = _plan(needs_research=["current price of X"])
    assert runner._research_and_replan(plan, "write a.md") is plan


def test_research_per_item_then_replan_with_notes(tmp_path):
    calls = []
    seen = {}

    def fake_researcher(item):
        calls.append(item)
        return [f"note about {item}"]

    runner = _runner(tmp_path, researcher=fake_researcher)
    original = _plan(needs_research=["price of X", "auth shape of Y"])

    def fake_plan(goal, spec=None, research_context=""):
        seen["research_context"] = research_context
        return _plan(deliverables=["b.md"])

    runner._plan = fake_plan
    out = runner._research_and_replan(original, "write a.md")
    assert calls == ["price of X", "auth shape of Y"]
    assert out.deliverables == ["b.md"]
    assert "note about price of X" in seen["research_context"]
    assert "note about auth shape of Y" in seen["research_context"]


def test_replan_through_real_plan_parses_notes(tmp_path):
    replies = [
        json.dumps({"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"},
                               "deliverable": "a.md"}],
                    "deliverables": ["a.md"], "criteria": [],
                    "needs_research": ["price of X"]}),
        json.dumps({"steps": [{"tool": "write", "args": {"file": "b.md", "text": "priced"},
                               "deliverable": "b.md"}],
                    "deliverables": ["b.md"], "criteria": []}),
    ]

    class SeqClient(FakeClient):
        def chat(self, messages, sampling=None):
            return replies.pop(0)

    runner = _runner(tmp_path, researcher=lambda item: ["X costs 5"],
                     client=SeqClient(""))
    original = runner._plan("write a.md")
    assert original.needs_research == ["price of X"]
    out = runner._research_and_replan(original, "write a.md")
    assert out.deliverables == ["b.md"]
    assert replies == [], "exactly one replan pass, no further research"


def test_replan_failure_keeps_original_and_records_note(tmp_path):
    runner = _runner(tmp_path, researcher=lambda item: ["note"])
    original = _plan(needs_research=["price of X"])

    def boom(goal, spec=None, research_context=""):
        raise ValueError("planner down")

    runner._plan = boom
    out = runner._research_and_replan(original, "write a.md")
    assert out is original
    assert "replan" in runner._replan_note
    report = runner._finalize(RunReport("failed", "write a.md", original,
                                        False, False))
    assert "replan" in report.replan_note


def test_replan_invalid_plan_keeps_original(tmp_path):
    runner = _runner(tmp_path, researcher=lambda item: ["note"])
    original = _plan(needs_research=["price of X"])
    runner._plan = lambda goal, spec=None, research_context="": Plan(
        steps=[], deliverables=[], criteria=[])
    out = runner._research_and_replan(original, "write a.md")
    assert out is original
    assert runner._replan_note


# ---- proven-run DAG learn ----------------------------------------------------

def _nodes():
    return [{"tool": "write", "args": {"file": "resume.md", "text": "cv"},
             "deliverable": "resume.md"}]


def test_learn_replaces_similar_entry_under_existing_id(tmp_path):
    store = DagStore(tmp_path)
    action, dag_id = store.learn("build my professional resume as a PDF", _nodes())
    assert (action, dag_id) == ("added", "build-professional-resume-pdf")
    assert len(store.list_dags()) == 1
    action, same_id = store.learn(
        "interview me about my capabilities then build my professional resume",
        [{"tool": "write", "args": {"file": "resume2.md", "text": "cv2"},
          "deliverable": "resume2.md"}])
    assert action == "replaced" and same_id == dag_id
    assert len(store.list_dags()) == 1, "no near-duplicate added"
    saved = store.get(dag_id)
    assert saved["nodes"][0]["deliverable"] == "resume2.md"


def test_learn_adds_distinct_shape_under_new_id(tmp_path):
    store = DagStore(tmp_path)
    store.learn("build my professional resume as a PDF", _nodes())
    action, dag_id = store.learn("plan a thanksgiving dinner for two", _nodes())
    assert action == "added" and dag_id != "build-professional-resume-pdf"
    assert len(store.list_dags()) == 2


def test_learn_rejects_empty_nodes(tmp_path):
    assert DagStore(tmp_path).learn("some goal", []) == ("invalid", "")


def test_finalize_success_learns_and_failure_does_not(tmp_path):
    runner = _runner(tmp_path)
    plan = _plan()
    done = runner._finalize(RunReport("done", "build my professional resume",
                                      plan, True, True))
    assert done.learned_dag.startswith("added:")
    assert len(DagStore(tmp_path).list_dags()) == 1
    failed = runner._finalize(RunReport("failed", "plan a dinner", plan,
                                        False, False))
    assert failed.learned_dag == ""
    assert len(DagStore(tmp_path).list_dags()) == 1, "failure learns nothing"


def test_finalize_learn_failure_never_fails_run(tmp_path):
    runner = _runner(tmp_path)
    plan = _plan()
    # A home that is a file, not a directory: DagStore cannot mkdir.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    runner.home = blocker
    report = runner._finalize(RunReport("done", "build my resume", plan,
                                        True, True))
    assert report.status == "done"
