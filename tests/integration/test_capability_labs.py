"""Lab interviews, saved briefs, and run handoffs without running external tools."""
import json
from types import SimpleNamespace

import pytest

from src.interface.webui import WebUIServer
from src.interface.interview_store import InterviewStore
from src.interface.webui_labs import approved_lab_for_conversation, lab_execution_plan
from src.interface.triggers import TriggerDispatcher

TOOL = {"name": "csv_cleaner", "purpose": "Clean the owner's CSV", "interface": {"input": "CSV string", "output": "CSV string"},
        "implementation": "Python standard library", "acceptance_tests": ["assert duplicate rows are removed"]}
SKILL = {"name": "normalize-names", "description": "Normalize names in a CSV", "markdown":
         "---\nname: normalize-names\ndescription: Normalize names in a CSV\n---\n\nApply strip and title case to names.\n\n## Tests\npython3 -c 'assert \" alice \".strip().title() == \"Alice\"'\n"}


def server(tmp_path):
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", lambda event: pytest.fail("An interview must not run the agent pipeline"))
    return WebUIServer(tmp_path, dispatcher=dispatcher)


def message(s, kind, text, revision=0, **kw):
    return s.handle_lab_message({"kind": kind, "message": text, "conversation_id": kind + "-interview", "revision": revision, **kw})


def test_interview_repeats_instructions_and_preserves_owner_info_after_corrections(tmp_path):
    s = server(tmp_path)
    seen = []
    replies = iter([{"reply": "Review this brief", "brief": TOOL}, {"reply": "Which column should stay?", "brief": None}])
    def model(messages):
        seen.append(messages)
        return json.dumps(next(replies))
    s.lab_model = model
    status, first = message(s, "tool", "Use stdlib, preserve row order, remove duplicate rows.")
    assert status == 200 and first["state"]["brief"] == TOOL
    status, second = message(s, "tool", "Actually keep duplicates with different timestamps.", first["revision"])
    assert status == 200 and second["state"]["brief"] is None
    assert "Use stdlib, preserve row order" in seen[1][1].content
    assert "Actually keep duplicates" in seen[1][1].content
    assert "read-only interview" in seen[0][0].content and seen[0][0].content == seen[1][0].content
    status, _ = s.handle_lab_start({"kind": "tool", "conversation_id": "tool-interview", "revision": second["revision"]})
    assert status == 400


def test_approved_skill_from_user_info_gets_its_own_build_and_exact_plan(tmp_path):
    s = server(tmp_path)
    s.lab_model = lambda messages: json.dumps({"reply": "Review the skill", "brief": SKILL})
    status, result = message(s, "skill", "Normalize CSV names using strip then title case.")
    assert status == 200 and result["state"]["brief"]
    builds = []
    def start(body):
        builds.append(body)
        return {"run_id": "build-run", "conversation_id": body["conversation_id"]}
    s.start_goal = start
    status, build = s.handle_lab_start({"kind": "skill", "conversation_id": "skill-interview", "revision": result["revision"]})
    assert status == 200 and "conversation_id=" in build["url"]
    assert build["conversation_id"] != "skill-interview"
    assert builds[0]["mode"] == "execute"
    approved = approved_lab_for_conversation(tmp_path, build["conversation_id"])
    assert approved == ("skill", SKILL)
    assert approved_lab_for_conversation(tmp_path, "skill-interview") is None
    assert approved_lab_for_conversation(tmp_path, "") is None
    plan = lab_execution_plan(*approved)
    assert plan.steps[0].args["text"] == SKILL["markdown"]
    assert plan.steps[-1].tool == "adopt_skill"
    assert plan.steps[-1].depends_on == [plan.steps[0].id]
    status, again = s.handle_lab_start({"kind": "skill", "conversation_id": "skill-interview", "revision": build["revision"]})
    assert status == 200 and again["run_id"] == "build-run" and len(builds) == 1


def test_cancel_stops_lab_without_model_and_clears_ready_brief(tmp_path):
    s = server(tmp_path)
    s.lab_model = lambda messages: json.dumps({"reply": "Ready", "brief": TOOL})
    _, first = message(s, "tool", "Clean CSV rows")
    s.lab_model = lambda messages: pytest.fail("Cancellation is handled without a model")
    status, cancelled = message(s, "tool", "reject", first["revision"])
    assert status == 200 and cancelled["state"]["closed"]
    assert cancelled["state"]["brief"] is None
    status, _ = message(s, "tool", "hello", cancelled["revision"])
    assert status == 400


def test_mcp_click_uses_saved_connection_and_rejects_invented_choice(tmp_path):
    s = server(tmp_path)
    option = {"id": "known", "name": "org.example/server", "title": "Example", "connection": {"transport": "http", "url": "https://example.com/mcp"}, "source": "https://registry.modelcontextprotocol.io/record"}
    revision = InterviewStore(tmp_path).save_lab("mcp", {"id": "mcp-interview", "turns": [{"role": "user", "text": "Read my notes, do not modify them"}], "options": [option]}, 0)
    s.lab_model = lambda messages: pytest.fail("Selecting a discovered server does not invent connection settings")
    status, _ = message(s, "mcp", "Use this", revision, choice="invented")
    assert status == 400
    status, result = message(s, "mcp", "Use this", revision, choice="known")
    assert status == 200
    brief = result["state"]["brief"]
    assert "Read my notes, do not modify them" in brief["requirements"]
    plan = lab_execution_plan("mcp", brief)
    assert plan.steps[0].tool == "mcp_connect"
    assert plan.steps[0].args["url"] == "https://example.com/mcp"


def test_skill_test_backend_rejects_missing_isolation_and_honors_failure(tmp_path):
    from src.capabilities.skill_test_backend import SkillTestBackend
    from src.capabilities.execution import ExecutionResult
    backend = SimpleNamespace(name="modal", allow_network=False,
        run=lambda *args, **kw: ExecutionResult(1, "", "assertion failed", True))
    sandbox = SkillTestBackend(backend, tmp_path)
    from src.capabilities.skill_import import _sandbox_run
    ok, output = _sandbox_run(sandbox, "python3 verify.py")
    assert not ok and "assertion failed" in output
    backend.allow_network = True
    with pytest.raises(ValueError): SkillTestBackend(backend, tmp_path)
