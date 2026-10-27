from src.system.main import _goal_extra_tools
from src.agentic.resume import save_run_state, resume_state
from src.agentic.plan_guard import validate_plan
from src.agentic.artifact_inventory import inventory_markdown
from src.agentic.delivery_summary import delivery_summary
from src.agentic.loop import (Plan, RunReport, format_duration, format_elapsed,
                              format_run_stats)


class _Cfg:
    github_token = ""
    threads_account_id = ""
    threads_access_token = ""
    ig_user_id = ""
    ig_access_token = ""
    fish_api_key = ""
    tts_voice_id = ""
    transcribe_api_key = ""
    owner_email = ""


def test_extra_tools_include_new_tools(tmp_path):
    tools = _goal_extra_tools(_Cfg(), tmp_path)
    assert "record_system_audio" in tools
    assert "write_versioned" in tools
    assert "make_spreadsheet" in tools
    assert "make_docx" in tools
    assert "make_slides" in tools
    assert "make_latex" in tools


def test_write_versioned_tool_roundtrip(tmp_path):
    tools = _goal_extra_tools(_Cfg(), tmp_path)
    assert tools["write_versioned"]("note.md", "hello") == "1"
    assert tools["write_versioned"]("note.md", "hello") == "1"
    assert tools["write_versioned"]("note.md", "changed") == "2"


def test_plan_guard_validates_file_plan(tmp_path):
    plan = {"steps": [{"id": "s1", "tool": "write", "args": {"file": "a.md"}, "deliverable": "a.md"}]}
    assert validate_plan(plan, ["write"]) == []


def test_resume_save_and_show(tmp_path):
    save_run_state(tmp_path, "abc123", {"goal": "g", "steps": [], "completed": ["a.md"], "status": "done"})
    ok, state, error = resume_state(tmp_path, "abc123")
    assert ok and state["goal"] == "g" and error == ""


def test_delivery_summary_render(tmp_path):
    (tmp_path / "a.md").write_text("x")
    text = delivery_summary({"goal": "g", "deliverables": {"a.md": True}, "files": ["a.md"], "elapsed": 5, "model_tokens_estimated": 1234, "repairs": 0})
    assert "a.md" in text and "g" in text
    assert "**Total workflow tokens:** 1,234 (estimated)" in text
    assert "a.md" in inventory_markdown(tmp_path)


def test_finished_run_stats_put_tokens_next_to_elapsed():
    report = RunReport("done", "g", Plan(), True, True,
                       elapsed_seconds=12.34,
                       model_tokens_estimated=5678)
    assert format_run_stats(report) == "⏱️ 12s · 5,678 total workflow tokens (estimated)"


def test_elapsed_helpers_keep_old_human_format():
    assert format_duration(124) == "2m 4s"
    assert format_duration(3600) == "1h 0m"
    assert "2m 4s" in format_elapsed({"elapsed_seconds": 124})
    assert "waiting for you" in format_elapsed({"elapsed_seconds": 40,
                                                "approval_wait_seconds": 180})
