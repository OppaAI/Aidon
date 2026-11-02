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
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.capabilities.tools import ToolRegistry
from src.capabilities.tools import ToolResult
from src.capabilities.extra_tools import dispatch_extra_tool
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


def test_tool_result_keeps_text_and_artifact_metadata(tmp_path):
    result = ToolResult("wrote /tmp/report.pdf", path=tmp_path / "report.pdf",
                        kind="document")
    assert str(result) == "wrote /tmp/report.pdf"
    assert result.path.endswith("report.pdf")
    assert result.kind == "document"
    assert result.status == "ok"
    dispatched = dispatch_extra_tool(
        {"make_file": lambda: result}, "make_file")
    assert dispatched.path.endswith("report.pdf")


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


def test_verify_normalizes_workdir_and_output_link_criteria(tmp_path):
    work = tmp_path / "workdir"
    work.mkdir()
    (work / "aidon_intro.pdf").write_bytes(b"%PDF-1.4")
    ok, failures, verified = verify_plan(
        work, ["workdir/aidon_intro.pdf"],
        ["PDF exists at workdir/aidon_intro.pdf",
         "Output message provides link to the PDF"])
    assert ok, failures
    assert verified == ["workdir/aidon_intro.pdf"]


def test_verify_recovers_single_pdf_from_prose_deliverable(tmp_path):
    work = tmp_path / "workdir"
    work.mkdir()
    (work / "Aidon_document_2026.pdf").write_bytes(b"%PDF-1.4")
    ok, failures, verified = verify_plan(
        work, ["Aidon introduction PDF"],
        ["PDF file exists inside workdir",
         "PDF link can be returned in output message"])
    assert ok, failures
    assert verified == ["Aidon_document_2026.pdf"]


def test_loop_executes_real_steps_and_verifies(tmp_path):
    plan = {"steps": [{"tool": "write", "args": {"file": "out.md", "text": "done"}, "deliverable": "out.md"}],
            "deliverables": ["out.md"], "criteria": ["out.md contains done"]}
    runner = _runner(tmp_path, FakeClient(json.dumps(plan)))
    report = runner.run("Write out")
    assert report.status == "done"
    assert report.files == ["out.md"]
    assert (tmp_path / "home" / "work" / "out.md").read_text() == "done"


def test_trusted_skill_replans_original_goal_and_keeps_completed_draft(
        tmp_path, monkeypatch):
    replanned = {
        "steps": [
            {"tool": "write", "args": {"file": "draft.md", "text": "kept"},
             "deliverable": "draft.md"},
            {"tool": "write", "args": {"file": "final.md", "text": "skill-informed"},
             "deliverable": "final.md"},
        ],
        "deliverables": ["draft.md", "final.md"],
        "criteria": ["final.md contains skill-informed"],
    }
    runner = _runner(tmp_path, FakeClient(json.dumps(replanned)))
    runner.extra_tools = {"adopt_skill": lambda: "not called directly"}
    initial = Plan(
        steps=[
            Step(tool="write", args={"file": "draft.md", "text": "kept"},
                 deliverable="draft.md"),
            Step(tool="adopt_skill", args={"name": "video-editing"}),
        ],
        deliverables=["draft.md"],
        parallel_ok=True,
    )

    class Plugins:
        trusted = False

        def tool_names(self):
            return []

        def skill_texts(self):
            if self.trusted:
                return {"video-editing": "Trusted, tested video editing procedure."}
            return {}

    runner.plugins = Plugins()
    original_call_tool = runner._call_tool
    calls = []

    def call_tool(name, args):
        calls.append(name)
        if name == "adopt_skill":
            runner.plugins.trusted = True
            return "Trusted skill video-editing; its tested procedure is available."
        return original_call_tool(name, args)

    monkeypatch.setattr(runner, "_call_tool", call_tool)
    report = runner._execute_plan("Create a launch video", initial)

    assert report.status == "done"
    assert calls.count("adopt_skill") == 1
    assert calls.count("write") == 2  # draft once; only the new final is rerun
    assert any("Trusted, tested video editing procedure" in message.content
               for message in runner.client.calls[-1])
    assert (tmp_path / "home" / "work" / "draft.md").read_text() == "kept"
    assert (tmp_path / "home" / "work" / "final.md").read_text() == "skill-informed"


def test_skill_replan_fails_closed_after_irreversible_step(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MAX_ATTEMPTS", "1")
    runner = _runner(tmp_path, FakeClient("{}"))
    runner.extra_tools = {"send_email": lambda: "sent",
                          "adopt_skill": lambda: "not called directly"}
    initial = Plan(
        steps=[
            Step(tool="send_email", args={}, deliverable="sent"),
            Step(tool="adopt_skill", args={"name": "video-editing"}),
        ],
        deliverables=["sent"],
    )
    monkeypatch.setattr(
        runner, "_call_tool",
        lambda name, args: ("Trusted skill video-editing" if name == "adopt_skill"
                            else "sent"))
    report = runner._execute_plan("Send the launch email", initial)
    assert report.status == "failed"
    assert "cannot be safely resumed" in report.error


def test_generated_file_path_replaces_prose_deliverable(tmp_path):
    plan = {"steps": [{
        "tool": "generate_document",
        "args": {"title": "Aidon introduction", "filename": "intro.pdf"},
        "deliverable": "Aidon introduction PDF",
        "criteria": "PDF file created inside workdir",
    }], "deliverables": ["Aidon introduction PDF"],
            "criteria": ["Output message provides link to the PDF"]}

    def generate_document(title, filename):
        path = tmp_path / "home" / "work" / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    runner = _runner(tmp_path, FakeClient(json.dumps(plan)))
    runner.extra_tools = {"generate_document": generate_document}
    report = runner.run("Create an introduction PDF")
    assert report.status == "done"
    assert report.files == ["intro.pdf"]


def test_edit_image_path_is_bound_to_workspace_deliverable(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_CRITIC_ENABLED", "0")
    runner = _runner(tmp_path, FakeClient("{}"))
    output = runner.sandbox.workdir / "cafe_scene.jpg"

    def edit_image(**_kwargs):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"edited image bytes")
        return f"IMAGE_SAVED: {output}"

    runner.extra_tools = {"edit_image": edit_image}
    plan = Plan(
        steps=[Step(tool="edit_image", args={}, deliverable="cafe_scene.jpg")],
        deliverables=["cafe_scene.jpg"],
        criteria=["cafe_scene.jpg exists and is non-empty"],
    )

    report = runner._execute_plan("Change the scene in the attached image", plan)
    assert report.status == "done", report.error
    assert report.files == ["cafe_scene.jpg"]


def test_document_goal_does_not_accept_list_only_plan(tmp_path):
    plan = {"steps": [{"tool": "list", "args": {},
                        "deliverable": "workdir_listing_verification"}],
            "deliverables": ["workdir_listing_verification"],
            "criteria": ["PDF is created inside workdir with auto-derived filename"]}

    def generate_document(title, content, format, filename=None):
        # The fallback now passes an explicit filename (the real tool
        # honors it); this stub keeps writing its derived path, which
        # is what the actual-path binding is supposed to map to.
        path = tmp_path / "home" / "work" / "derived.pdf"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
        return str(path)

    runner = _runner(tmp_path, FakeClient(json.dumps(plan)))
    runner.extra_tools = {"generate_document": generate_document}
    report = runner.run("Write a detailed introduction in a PDF file")
    assert report.status == "done"
    assert report.files == ["derived.pdf"]


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


def test_loop_with_memory_records_chat_and_experience(tmp_path):
    memory = Memory(tmp_path / "home", embedder=HashEmbedder(), store=InMemoryStore())
    plan = {"steps": [{"tool": "write", "args": {"file": "out.md", "text": "hi"}, "deliverable": "out.md"}],
            "deliverables": ["out.md"], "criteria": []}
    runner = _runner(tmp_path, FakeClient(json.dumps(plan)), memory=memory)
    runner.conversation_id = "dinner"
    report = runner.run("Write out")
    assert report.status == "done"
    episodic = memory.list_points("episodic")
    assert episodic and episodic[0].payload["conversation_id"] == "dinner"
    assert memory.list_points("experience")


def test_capability_interview_recovers_durable_answer_after_restart(tmp_path):
    from src.interface.interview_store import InterviewStore

    home = tmp_path / "home"
    home.mkdir()
    store = InterviewStore(home)
    first = GoalRunner(
        FakeClient('{"question":"Which output format?"}'),
        Sandbox(home / "work-first"),
        ApprovalGate(lambda *args, **kwargs: Decision("approved")),
        home, conversation_id="chat-recovery", run_id="run-before-crash")

    def interrupted_ask(prompt):
        if prompt.startswith("[AIDON_LAB] "):
            question = prompt.removeprefix("[AIDON_LAB] ")
            turn_id = store.question(
                first.run_id, first.conversation_id, question, True)
            store.answer(turn_id, "MP4")
            raise RuntimeError("simulated process interruption")
        return "proceed"

    first.question_ask = interrupted_ask
    try:
        first._capability_interview("Create a launch video", "need_skill")
        assert False, "interview should be interrupted after its answer is saved"
    except RuntimeError as exc:
        assert "simulated process interruption" in str(exc)

    recovered = GoalRunner(
        FakeClient('{"brief":"Draft an MP4 editing procedure."}'),
        Sandbox(home / "work-recovered"),
        ApprovalGate(lambda *args, **kwargs: Decision("approved")),
        home, conversation_id="chat-recovery", run_id="run-after-restart")
    questions = []

    def resume_ask(prompt):
        questions.append(prompt)
        return "proceed"

    recovered.question_ask = resume_ask
    outcome, brief = recovered._capability_interview(
        "Create a launch video", "need_skill")

    assert outcome == "approved"
    assert brief == "Draft an MP4 editing procedure."
    assert not any("Which output format?" in prompt for prompt in questions)
    assert "MP4" in recovered.client.calls[-1][-1].content
    state = store.unfinished_for_conversation(
        "chat-recovery", goal="Create a launch video")
    assert state["outcome"] == "approved" and state["finished"] is False


def test_recovering_approved_acquisition_does_not_repeat_consent(tmp_path):
    from src.interface.interview_store import InterviewStore

    home = tmp_path / "home"
    home.mkdir()
    first_store = InterviewStore(home)
    first = GoalRunner(
        FakeClient('{"brief":"Use an MP4 editing checklist."}'),
        Sandbox(home / "work-first"),
        ApprovalGate(lambda *args, **kwargs: Decision("approved")),
        home, conversation_id="approved-recovery", run_id="before-restart")
    first.question_ask = lambda prompt: "proceed"
    outcome, _ = first._capability_interview("Prepare video", "need_skill")
    assert outcome == "approved"

    recovered = GoalRunner(
        FakeClient('{"brief":"A different brief must not replace consent."}'),
        Sandbox(home / "work-after-restart"),
        ApprovalGate(lambda *args, **kwargs: Decision("rejected")),
        home, conversation_id="approved-recovery", run_id="after-restart")
    recovered.question_ask = lambda prompt: (_ for _ in ()).throw(
        AssertionError("saved interview must replay without another question"))
    resumed, brief = recovered._capability_interview("Prepare video", "need_skill")
    assert resumed == "approved"
    assert brief == "Use an MP4 editing checklist."
    state = first_store.unfinished_for_conversation(
        "approved-recovery", goal="Prepare video")
    assert state["finished"] is False
    assert state["outcome"] == "approved"


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
