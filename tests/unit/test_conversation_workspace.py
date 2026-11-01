"""Conversation workspaces carry verified artifacts without reusing sandboxes."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.conversation_workspace import ConversationWorkspace


def test_verified_artifacts_and_workflow_stage_into_later_run(tmp_path):
    home = tmp_path / "home"
    first_run = tmp_path / "run-one"
    first_run.mkdir()
    (first_run / "grocery-plan.md").write_text("Meal plan\nShopping list\n")
    (first_run / "scratch.txt").write_text("not verified")

    store = ConversationWorkspace(home, "conversation-1")
    published = store.publish(
        first_run,
        ["grocery-plan.md", "../outside.txt", str(tmp_path / "outside.txt")],
        {"goal": "Create a grocery plan", "summary": "Plan and price meals",
         "criteria": ["under $40"],
         "steps": [{"tool": "write", "description": "Create the plan"}]})
    assert published == ["grocery-plan.md"]

    next_run = tmp_path / "run-two"
    next_run.mkdir()
    restored = store.stage(next_run)
    assert (next_run / "grocery-plan.md").read_text() == "Meal plan\nShopping list\n"
    assert not (next_run / "scratch.txt").exists()
    assert restored["artifacts"] == ["grocery-plan.md"]
    assert restored["workflow"]["goal"] == "Create a grocery plan"
    assert restored["workflow"]["steps"] == [
        {"tool": "write", "description": "Create the plan"}]


def test_conversation_artifacts_are_isolated(tmp_path):
    home = tmp_path / "home"
    run = tmp_path / "run"
    run.mkdir()
    (run / "note.md").write_text("private to conversation one")
    ConversationWorkspace(home, "conversation-one").publish(
        run, ["note.md"], {"goal": "Make a note"})

    other_run = tmp_path / "other-run"
    other_run.mkdir()
    restored = ConversationWorkspace(home, "conversation-two").stage(other_run)
    assert restored["artifacts"] == []
    assert not (other_run / "note.md").exists()
