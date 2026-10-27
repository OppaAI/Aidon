"""#2 plan execution tests: tools, verifier, repair, wiring.

Offline, no model, no Qdrant server. FakeClient returns
plan JSON, then repaired plan JSON on the second call.
"""

import json

from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.memory.kb import KnowledgeBase, parser_needed_for
from src.agentic.loop import GoalRunner, Plan, Step
from src.memory.memory import InMemoryStore, Memory
from src.core.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.capabilities.tools import ToolRegistry
from src.agentic.verify import check_criterion, verify_plan


def _runner(tmp_path, client, memory=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(client, sandbox, gate, home, memory=memory)


def test_tools_write_read_calculate_ledger(tmp_path):
    work = tmp_path / "work"
    registry = ToolRegistry(work)
    assert registry.call("write", {"file": "note.md", "text": "hello"}) == "wrote note.md"
    assert registry.call("read", {"file": "note.md"}) == "hello"
    assert registry.call("calculate", {"expression": "2 + 3 * 4"}) == "14.0"
    registry.call("write", {"file": "ledger.json",
                            "text": json.dumps({"lines": [{"amount_cents": 2000}, {"amount_cents": 500}]})})
    assert registry.call("calculate", {"ledger": "ledger.json"}) == "2500"
    # Outside workdir is rejected, never followed.
    try:
        registry.call("write", {"file": "../escape.md", "text": "x"})
        assert False, "should have raised"
    except ValueError:
        pass


def test_verify_criteria_and_ledger(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.md").write_text("roast beef")
    (work / "ledger.json").write_text(json.dumps({"lines": [{"amount_cents": 100}, {"amount_cents": 200}]}))
    ok, failures, verified = verify_plan(work, ["a.md"], ["a.md exists", "a.md contains roast beef", "ledger ledger.json total is 300"])
    assert ok and not failures and verified == ["a.md"]
    ok2, failures2, _ = verify_plan(work, ["a.md"], ["ledger ledger.json total is 999"])
    assert not ok2 and failures2
    ok3, _, _ = verify_plan(work, ["missing.md"], [])
    assert not ok3


def test_loop_executes_real_steps_and_verifies(tmp_path):
    plan = {"steps": [{"tool": "write", "args": {"file": "out.md", "text": "done"}, "deliverable": "out.md"}],
            "deliverables": ["out.md"], "criteria": ["out.md contains done"]}
    runner = _runner(tmp_path, FakeClient(json.dumps(plan)))
    report = runner.run("Write out")
    assert report.status == "done"
    assert report.files == ["out.md"]
    assert (tmp_path / "home" / "work" / "out.md").read_text() == "done"


def test_loop_repair_keeps_passed_files(tmp_path):
    # First plan: good file + bad criterion (ledger total wrong).
    # Repair plan: fixes only the ledger, good file must survive untouched.
    first = {"steps": [
        {"tool": "write", "args": {"file": "good.md", "text": "keep me"}, "deliverable": "good.md"},
        {"tool": "write", "args": {"file": "ledger.json", "text": json.dumps({"lines": [{"amount_cents": 100}]})}, "deliverable": "ledger.json"},
    ], "deliverables": ["good.md", "ledger.json"],
      "criteria": ["ledger ledger.json total is 999"]}
    repaired = {"steps": [
        {"tool": "write", "args": {"file": "ledger.json", "text": json.dumps({"lines": [{"amount_cents": 999}]})}, "deliverable": "ledger.json"},
    ], "deliverables": ["good.md", "ledger.json"],
      "criteria": ["ledger ledger.json total is 999"]}

    class RepairClient(FakeClient):
        def __init__(self):
            self.calls = 0
        def chat(self, messages):
            self.calls += 1
            return json.dumps(first if self.calls == 1 else repaired)

    runner = _runner(tmp_path, RepairClient())
    report = runner.run("Repair test")
    assert report.status == "done"
    assert report.attempts == 2
    assert (tmp_path / "home" / "work" / "good.md").read_text() == "keep me"
    assert json.loads((tmp_path / "home" / "work" / "ledger.json").read_text())["lines"][0]["amount_cents"] == 999


def test_loop_with_memory_records_project_and_experience(tmp_path):
    memory = Memory(tmp_path / "home", embedder=HashEmbedder(), store=InMemoryStore())
    plan = {"steps": [{"tool": "write", "args": {"file": "out.md", "text": "hi"}, "deliverable": "out.md"}],
            "deliverables": ["out.md"], "criteria": []}
    runner = _runner(tmp_path, FakeClient(json.dumps(plan)), memory=memory)
    runner.project_id = "dinner"
    report = runner.run("Write out")
    assert report.status == "done"
    episodic = memory.list_points("episodic")
    assert episodic and episodic[0].payload["project_id"] == "dinner"
    assert memory.list_points("experience")


def test_kb_parser_needed_detection(tmp_path):
    assert parser_needed_for(tmp_path / "model.dwg") == "cad-parser"
    assert parser_needed_for(tmp_path / "paper.tex") == "latex-parser"
    assert parser_needed_for(tmp_path / "note.txt") is None
    memory = Memory(tmp_path / "home", embedder=HashEmbedder(), store=InMemoryStore())
    kb = KnowledgeBase(memory)
    cad = tmp_path / "model.dwg"
    cad.write_bytes(b"fake cad")
    target = kb.import_file(cad)
    assert target.exists()
    hits = kb.recall("model")
    assert hits and hits[0].point.payload.get("parser_needed") == "cad-parser"
