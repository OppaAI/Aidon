"""#1 interview tests: chat questions, frozen spec, approval card.

Questions are chat turns, no gate, no clock. The spec
approval is a decision, through the gate. FakeClient
returns question JSON, then a completed spec JSON.
"""

import json

from src.interface.approval import ApprovalGate, Decision
from src.memory.embedder import HashEmbedder
from src.agentic.interview import (InterviewRunner, Spec, parse_spec_sheet,
                           spec_sheet, spec_sheet_md, SPEC_SHEET_TEMPLATE)
from src.agentic.loop import GoalRunner
from src.memory.memory import InMemoryStore, Memory
from src.core.model_client import FakeClient
from src.capabilities.sandbox import Sandbox


def _memory(tmp_path):
    return Memory(tmp_path / "home", embedder=HashEmbedder(),
                  store=InMemoryStore())


def test_spec_verify():
    good = Spec(goal="Plan a dinner", deliverables=["menu.pdf"],
                constraints=["no milk", "no eggs"], budget_cents=10000)
    assert good.verify()[0] is True
    bad = Spec(goal="Plan a dinner", deliverables=[],
               open_questions=["what is the budget?"])
    ok, failures = bad.verify()
    assert not ok and failures
    bad2 = Spec(goal="x", deliverables=["TBD"], budget_cents=-1)
    assert not bad2.verify()[0]


class _InterviewClient(FakeClient):
    def __init__(self):
        self.calls = 0
    def chat(self, messages):
        self.calls += 1
        if self.calls == 1:
            return json.dumps({"question": "What is the main dish?",
                               "spec": {"goal": "Plan a dinner", "deliverables": []}})
        return json.dumps({"question": None, "complete": True,
                           "spec": {"goal": "Plan a dinner",
                                    "deliverables": ["menu.pdf"],
                                    "constraints": ["no milk", "no eggs"],
                                    "budget_cents": 10000}})


def test_interview_chat_then_spec_approval(tmp_path):
    memory = _memory(tmp_path)
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = InterviewRunner(_InterviewClient(), tmp_path / "home",
                             memory=memory, gate=gate, project_id="dinner")
    answers = iter(["roast beef"])
    result = runner.start_from_message(
        "Plan a dinner", ask=lambda q: next(answers))
    assert result.verified is True
    assert result.approved is True
    assert result.spec.deliverables == ["menu.pdf"]
    assert result.spec.budget_cents == 10000
    # Spec saved, Q&A in STM and episodic.
    assert (tmp_path / "home" / "specs" / "dinner.json").exists()
    assert memory.list_points("episodic")
    assert len(memory.stm) >= 2


def test_interview_unverified_spec_is_not_approved(tmp_path):
    class BadClient(FakeClient):
        def chat(self, messages):
            return json.dumps({"question": None, "complete": True,
                               "spec": {"goal": "Plan a dinner",
                                        "deliverables": []}})
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = InterviewRunner(BadClient(), tmp_path / "home",
                             memory=None, gate=gate, project_id="dinner")
    result = runner.start_from_message("Plan a dinner", ask=lambda q: "x")
    assert result.verified is False
    assert result.approved is False
    assert result.failures


def test_spec_sheet_markdown_round_trip(tmp_path):
    spec = Spec(goal="Plan a dinner", deliverables=["menu.pdf"],
                constraints=["no milk", "no eggs"], budget_cents=10000)
    sheet = spec_sheet_md(spec)
    assert sheet.startswith("# Spec Sheet")
    assert "## Deliverables" in sheet
    assert "$100.00" in sheet
    assert "menu.pdf" in sheet
    parsed = parse_spec_sheet(sheet)
    assert parsed.goal == "Plan a dinner"
    assert parsed.deliverables == ["menu.pdf"]
    assert parsed.constraints == ["no milk", "no eggs"]
    assert parsed.budget_cents == 10000


def test_user_written_markdown_sheet_parses(tmp_path):
    text = ("# Spec Sheet\n\n## Goal\n\nPlan a dinner\n\n"
            "## Deliverables\n\n- menu.pdf\n\n"
            "## Constraints\n\n- no milk\n\n## Budget\n\n$100.00\n")
    parsed = parse_spec_sheet(text)
    assert parsed.goal == "Plan a dinner"
    assert parsed.deliverables == ["menu.pdf"]
    assert parsed.constraints == ["no milk"]
    assert parsed.budget_cents == 10000


def test_attach_skips_interview_and_saves_markdown(tmp_path):
    memory = _memory(tmp_path)
    seen = {}
    def decide(kind, title, body):
        seen["body"] = body
        return Decision("approved")
    gate = ApprovalGate(decide)
    runner = InterviewRunner(FakeClient("should not be called"),
                             tmp_path / "home", memory=memory, gate=gate,
                             project_id="dinner")
    sheet = spec_sheet_md(Spec(goal="Plan a dinner",
                               deliverables=["menu.pdf"],
                               constraints=["no milk"]))
    result = runner.load_attached_spec(sheet)
    assert result.verified is True
    assert result.approved is True
    assert result.exchanges == []
    assert seen["body"].startswith("# Spec Sheet")
    assert (tmp_path / "home" / "specs" / "dinner.md").exists()
    assert (tmp_path / "home" / "specs" / "dinner.json").exists()
    template = runner.ensure_template()
    assert template.exists()
    assert template.read_text().startswith("# Spec Sheet")
    assert "attach it" in template.read_text()


def test_interview_prefills_from_previous_spec(tmp_path):
    memory = _memory(tmp_path)
    memory.record_episodic("dinner", "Interview spec: Plan a dinner",
                           {"spec": {"goal": "Plan a dinner",
                                     "deliverables": ["menu.pdf"],
                                     "constraints": ["no milk"]}})
    class EmptySpecClient(FakeClient):
        def chat(self, messages):
            return json.dumps({"question": None, "complete": True,
                               "spec": {"goal": "Plan a dinner",
                                        "deliverables": []}})
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = InterviewRunner(EmptySpecClient(), tmp_path / "home",
                             memory=memory, gate=gate, project_id="dinner")
    result = runner.start_from_message("Plan a dinner", ask=lambda q: "x")
    assert result.spec.deliverables == ["menu.pdf"]
    assert result.spec.constraints == ["no milk"]


def test_example_spec_sheet_parses_and_reconciles(tmp_path):
    example = (tmp_path / "repo").parent
    # Example file lives in the repo examples/ path, read
    # from the local workdir, not from a test fixture.
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    text = (repo / "examples" / "SPEC-SHEET-EXAMPLE.md").read_text()
    parsed = parse_spec_sheet(text)
    assert parsed.verify()[0] is True
    assert "October 2026" in parsed.goal
    assert "November 15, 2026" in " ".join(parsed.constraints)
    assert any("summary.csv" in d for d in parsed.deliverables)
    # Special reminder: one customer's PDF only to its own email.
    assert "SPECIAL REMINDER" in " ".join(parsed.constraints)
    assert "wrong email" in " ".join(parsed.constraints)
    assert any("only that customer's statement PDF" in d for d in parsed.deliverables)
    # Totals in the example, recomputed, not trusted from text.
    assert 755.50 + 2020.00 == 2775.50


def test_template_has_no_amounts_or_example_constraints(tmp_path):
    import pathlib
    repo = pathlib.Path(__file__).resolve().parents[1]
    text = (repo / "examples" / "SPEC-SHEET-TEMPLATE.md").read_text()
    assert "$100" not in text
    assert "no milk" not in text.lower()
    assert "no eggs" not in text.lower()
    assert "Write the goal" in text
    assert "Write the maximum" in text


def test_loop_with_spec_binds_deliverables(tmp_path):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    spec = Spec(goal="Write a note", deliverables=["spec-note.md"])
    plan = {"steps": [{"tool": "write", "args": {"file": "spec-note.md", "text": "from spec"}, "deliverable": "spec-note.md"}],
            "deliverables": ["spec-note.md"], "criteria": []}
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    runner = GoalRunner(FakeClient(json.dumps(plan)), Sandbox(home / "work"),
                        gate, home, memory=None)
    report = runner.run("Write a note", spec=spec)
    assert report.status == "done"
    assert report.files == ["spec-note.md"]
