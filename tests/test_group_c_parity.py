"""Group C parity: conversations DB, registry DB, intake schema,
pattern/prompt/planner/eval depth. All test doubles are local;
no model call, no network."""

import re
import threading

from src.memory.conversations_db import ConversationStoreDB, record_run_exchange
from src.capabilities.registry_db import Registry
from src.agentic import intake_schema as intake
from src.agentic.patterns import (
    Pattern, default_patterns, extract_destination, extract_destinations,
    extract_slot, extract_text_kind, fill_slots, instantiate,
    looks_like_question, match_pattern, score_pattern,
)
from src.agentic.prompts import (
    build_digest_prompt, build_minimal_prompt, build_system_prompt,
    capability_block, context_block, guidance_block, render_prompt,
    workspace_block,
)
from src.agentic.planner import (
    PlanSpec, PlanStep, UnknownToolError, _extract_json, _repair_json,
    make_plan, plan_goal, verify_acceptance,
)
from src.agentic.eval import (
    EvalHarness, EvalResult, eval_run, evaluate, grade_report,
)


# -- conversations DB ---------------------------------------------------------

def test_conversations_db_numbering_is_serialised(tmp_path):
    store = ConversationStoreDB(tmp_path)
    project = store.ensure_default_project()
    assert store.ensure_default_project()["id"] == project["id"]
    conv = store.create_conversation(project["id"])
    errors = []

    def record(n):
        try:
            for i in range(10):
                store.record_exchange(conv["id"], "user", f"turn {n}-{i}.")
        except Exception as exc:  # noqa: BLE001 - collected, asserted below
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    history = store.history(conv["id"])
    assert len(history) == 40
    numbers = [row["exchange_no"] for row in history]
    assert numbers == list(range(1, 41))
    store.close()


def test_conversations_db_history_summary_delete(tmp_path):
    store = ConversationStoreDB(tmp_path)
    project = store.create_project("dinner")
    conv = store.create_conversation(project["id"])
    record_run_exchange(store, conv["id"], "user",
                        "Plan a roast beef dinner for two.")
    record_run_exchange(store, conv["id"], "assistant",
                        "What is the budget for the roast beef?")
    history = store.history(conv["id"], limit=1)
    assert len(history) == 1
    assert history[0]["text"].startswith("What is the budget")
    summary = store.rolling_summary(conv["id"])
    assert "roast beef" in summary.lower()
    assert "budget" in summary.lower()
    assert store.list_conversations(project["id"])
    assert store.add_attachment(conv["id"], "menu.pdf", kind="pdf")["path"] == "menu.pdf"
    assert store.add_idea(project["id"], "try yorkshire pudding")["text"]
    assert store.delete_conversation(conv["id"]) is True
    assert store.history(conv["id"]) == []
    assert store.list_conversations(project["id"]) == []
    store.close()


# -- registry DB --------------------------------------------------------------

def test_registry_db_run_and_skill_version_round_trip(tmp_path):
    reg = Registry(tmp_path)
    assert reg.get_run("missing") is None
    record = reg.record_run("run-1", "plan dinner", state="done",
                            workspace="/tmp/work")
    assert record["run_id"] == "run-1" and record["state"] == "done"
    updated = reg.update_run("run-1", state="in_progress")
    assert updated["state"] == "in_progress"
    assert updated["workspace"] == "/tmp/work"
    assert len(reg.list_runs()) == 1
    assert len(reg.list_runs(state="done")) == 0
    reg.record_skill_version("menu", 1, {"steps": 2})
    reg.record_skill_version("menu", 2, {"steps": 3})
    versions = reg.list_skill_versions("menu")
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[1]["snapshot"]["steps"] == 3
    assert reg.list_skill_versions("other") == []
    mission = reg.record_mission("m-1", "dinner", state="ready_for_review")
    assert mission["state"] == "ready_for_review"
    assert reg.get_mission("m-1")["goal"] == "dinner"
    assert len(reg.list_missions()) == 1
    reg.close()


# -- intake schema --------------------------------------------------------------

def _slot(schema, name):
    return intake._slot(schema, name)


def test_intake_schema_missing_ready_and_coerce():
    schema = intake.get_schema("dinner")
    assert schema is not None and intake.get_schema("nope") is None
    spec = {}
    assert [s["name"] for s in intake.missing_slots(schema, spec)] == [
        "main", "guests", "budget_cents", "allergies", "store"]
    assert intake.ready(schema, spec) is False
    assert intake.coerce_value(_slot(schema, "guests"), "2 people") == (True, 2)
    assert intake.coerce_value(_slot(schema, "guests"), "zero") == (False, None)
    assert intake.coerce_value(_slot(schema, "budget_cents"), "$100.00") == (True, 10000)
    assert intake.coerce_value(_slot(schema, "budget_cents"), 10000) == (True, 10000)
    assert intake.coerce_value(_slot(schema, "style"), "family style") == (True, "family_style")
    assert intake.coerce_value(_slot(schema, "style"), "bogus") == (False, None)
    assert intake.coerce_value(_slot(schema, "allergies"), "milk and eggs") == (
        True, ["milk", "eggs"])
    assert intake.slot_value(spec, "main") is None


def test_intake_grounded_extraction_defaults_and_public_spec():
    schema = intake.get_schema("dinner")
    spec = {}
    # "sushi" was never said, so it is dropped; the store was said.
    applied = intake.apply_extraction(
        schema, spec, {"main": "sushi", "store": "Save-On-Foods"},
        source_text="roast beef at Save-On-Foods")
    assert applied == ["store"]
    assert "main" not in spec
    # A prefilled allergy value alone does not fill a confirm slot.
    assert intake.is_filled(schema, {"allergies": ["milk"]},
                            _slot(schema, "allergies")) is False
    assert intake.apply_default(spec, _slot(schema, "style")) is True
    assert intake.apply_default(spec, _slot(schema, "allergies")) is False
    public = intake.public_spec(schema, spec)
    assert public["style"] == "family_style"
    assert public["assumed_defaults"] == ["style"]
    assert "_assumed" not in public
    lines = intake.summary_lines(schema, spec)
    assert any("family style" in line and "assumed" in line for line in lines)


def test_intake_run_drives_callbacks_until_ready():
    answers = iter(["roast beef", "2", "10000", "milk and eggs", "Save-On-Foods"])

    def ask(question):
        if "what I've got" in question:
            return ("approved", "", "human")
        return ("approved", next(answers), "human")

    def extract(transcript):
        out = {}
        low = transcript.lower()
        if "roast beef" in low:
            out["main"] = "roast beef"
        if "milk and eggs" in low:
            out["allergies"] = "milk and eggs"
        if "save-on-foods" in low:
            out["store"] = "Save-On-Foods"
        if re.search(r"\b2\b", low):
            out["guests"] = "2"
        if "10000" in low:
            out["budget_cents"] = "10000"
        return out

    result = intake.run_intake(
        intake.get_schema("dinner"), "plan a dinner", ask=ask,
        extract=extract, word=lambda slot, spec, last: slot["ask"])
    assert result == {"main": "roast beef", "guests": 2, "budget_cents": 10000,
                      "allergies": ["milk", "eggs"], "store": "Save-On-Foods"}

    def ask_reject(question):
        return ("rejected", "", "human")

    assert intake.run_intake(
        intake.get_schema("dinner"), "plan", ask=ask_reject,
        extract=lambda transcript: {},
        word=lambda slot, spec, last: slot["ask"]) is None


# -- patterns -----------------------------------------------------------------

def test_patterns_extraction_scoring_and_instantiation():
    assert extract_text_kind("write a haiku about rain") == "haiku"
    assert extract_text_kind("write something") is None
    assert extract_slot("file", "save it to notes/report.md") == "notes/report.md"
    assert extract_slot("topic", "write a haiku about rain to rain.md") == "rain"
    assert extract_destinations("read it aloud and save it to x.md") == ["voice", "file"]
    assert extract_destination("just tell me") == "screen"
    assert looks_like_question("what is the price?") is True
    assert looks_like_question("write a poem") is False
    patterns = default_patterns()
    assert len(patterns) >= 6
    names = [p.name for p in patterns]
    assert "text" in names and "research_text" in names and "question" in names
    text_pattern = next(p for p in patterns if p.name == "text")
    assert score_pattern(text_pattern, "write a haiku about rain") > 0.5
    values = fill_slots(text_pattern, "write a haiku about rain to rain.md")
    assert values["kind"] == "haiku" and values["topic"] == "rain"
    assert values["file"] == "rain.md"
    dag = instantiate(text_pattern, values)
    assert dag["nodes"][0]["args"]["prompt"].startswith("Write a haiku about rain")
    assert dag["acceptance_criteria"][0].startswith("a file exists at rain.md")
    # The old dict patterns still work exactly as before.
    assert match_pattern("write a file")["name"] == "write_file"
    assert isinstance(Pattern(name="x", description="d").to_dict(), dict)


# -- prompts ------------------------------------------------------------------

def test_prompts_builders_and_blocks():
    system = build_system_prompt(persona="Be kind.", manual="## Capability ladder\nladder\n## Hard rules\nrules\n", user_text="User is Oppa.")
    assert system.startswith("You are Aidon")
    assert "Be kind." in system and "ladder" in system and "Oppa" in system
    digest = build_digest_prompt(manual="## Capability ladder\nladder\n## Hard rules\nrules\n", user_text="")
    assert "Capability ladder" in digest and "Hard rules" in digest
    minimal = build_minimal_prompt("research", user_text="")
    assert "research phase" in minimal and "Current time:" in minimal
    assert guidance_block("") == ""
    assert "binding" in guidance_block("stay under budget")
    assert context_block("dinner for two").startswith("Context:")
    assert workspace_block("/tmp/work").startswith("Workspace:")
    assert capability_block("write, read").startswith("Available capabilities:")
    assert context_block("") == "" and workspace_block("") == "" and capability_block("") == ""
    assert render_prompt("interpret", {"goal": "dinner"}).endswith("dinner")


# -- planner ------------------------------------------------------------------

class _FakeClient:
    def __init__(self, texts):
        self.texts = texts
        self.index = 0

    def chat(self, messages):
        text = self.texts[min(self.index, len(self.texts) - 1)]
        self.index += 1
        return type("Resp", (), {"text": text})()


def test_planner_json_repair():
    # Trailing comma, stray regex escape, and a code fence, all at once.
    raw = '```json\n{"title": "T", "steps": [], "acceptance_criteria": ["a"],}\n```'
    assert _extract_json(raw) == {"title": "T", "steps": [],
                                  "acceptance_criteria": ["a"]}
    assert _extract_json('{"cmd": "grep \\$price \\d+"}')["cmd"] == "grep \\$price \\d+"
    assert _repair_json('{"a": 1,}') == '{"a": 1}'


def test_planner_make_plan_and_acceptance():
    plan_json = ('{"title": "Dinner plan", "steps": ['
                 '{"id": "s1", "title": "Write menu", "tool": "write",'
                 ' "args": {"file": "menu.md"}, "deps": []}],'
                 ' "acceptance_criteria": ["menu exists"]}')
    client = _FakeClient([plan_json])
    plan = make_plan(client, "plan a dinner", tool_names=["write"])
    assert isinstance(plan, PlanSpec)
    assert plan.title == "Dinner plan"
    assert plan.steps[0].tool == "write"
    assert plan.to_dag().validate() == []
    assert plan.render().startswith("# Plan: Dinner plan")
    assert PlanSpec.from_dict(plan.to_dict()).title == "Dinner plan"
    assert PlanStep.from_dict(plan.steps[0].to_dict()).id == "s1"
    assert plan.render_candidates() == "(no alternatives listed)"
    # Unknown tool is a named error, not a generic failure.
    bad = _FakeClient(['{"title": "T", "steps": [{"id": "s1", "title": "X",'
                       ' "tool": "teleport", "args": {}, "deps": []}]}'])
    try:
        make_plan(bad, "go", tool_names=["write"])
    except UnknownToolError as exc:
        assert exc.tool == "teleport"
    else:  # pragma: no cover
        raise AssertionError("expected UnknownToolError")
    # An unparseable first reply gets one corrective retry.
    retry = _FakeClient(["not json at all", plan_json])
    assert make_plan(retry, "plan a dinner", tool_names=["write"]).title == "Dinner plan"
    verdict = verify_acceptance(
        _FakeClient(['{"passed": true, "results": []}']),
        "plan a dinner", ["menu exists"], "menu.md written")
    assert verdict["passed"] is True
    unparseable = verify_acceptance(_FakeClient(["garbage"]), "g", ["c"], "ev")
    assert unparseable["passed"] is False and unparseable["missing"] == ["c"]
    # The old deterministic planner still works.
    assert plan_goal("write a file")["pattern"] == "write_file"


# -- eval ---------------------------------------------------------------------

def test_eval_per_criterion_evidence_and_harness():
    result = evaluate(["menu", "roast beef", "missing dish"],
                      "the menu has roast beef and sides")
    assert result["passed"] == ["menu", "roast beef"]
    assert result["failed"] == ["missing dish"]
    assert result["score"] == 2 / 3
    assert result["ok"] is False
    assert isinstance(result["results"][0], EvalResult)
    assert "roast beef" in result["results"][1].evidence
    assert result["results"][2].evidence == ""
    assert evaluate([], "anything")["ok"] is False
    assert evaluate(["menu.md"], {}, files={"menu.md": "content"})["ok"] is True
    grade = grade_report(result)
    assert grade == {"total": 3, "passed": 2, "failed": 1,
                     "score": 2 / 3, "ok": False}
    harness = EvalHarness()
    aggregate = harness.run([
        {"criteria": ["x"], "evidence": "x y"},
        {"criteria": ["z"], "evidence": "nothing"},
        (["w"], "w here"),
    ])
    assert aggregate["total"] == 3 and aggregate["passed"] == 2
    assert aggregate["score"] == 2 / 3 and aggregate["ok"] is False
    assert len(aggregate["runs"]) == 3
    assert eval_run(["menu"], {"menu.md": "menu content"})["ok"] is True
