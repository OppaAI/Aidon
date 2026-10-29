"""Surface links, mirror commands, cross-surface questions, practice API."""

import threading
import time

from src.interface.surfaces import (
    conversation_for_slack, link_slack, mirror_status_text,
    parse_mirror_command, set_whatsapp_mirror, slack_for_conversation,
    unlink_slack, waiter_keys_for_conversation, waiter_keys_for_slack,
    whatsapp_mirrored_for_conversation, whatsapp_mirrored_for_slack)
from src.interface.webui import WebUIServer


def test_parse_mirror_command():
    assert parse_mirror_command("/mirror slack #general") == {
        "action": "link_slack", "channel": "general"}
    assert parse_mirror_command("/mirror slack off") == {"action": "unlink_slack"}
    assert parse_mirror_command("/mirror webui abc123") == {
        "action": "link_webui", "conversation": "abc123"}
    assert parse_mirror_command("/mirror whatsapp on") == {
        "action": "whatsapp", "enabled": True}
    assert parse_mirror_command("/mirror whatsapp off") == {
        "action": "whatsapp", "enabled": False}
    assert parse_mirror_command("/mirror") == {"action": "status"}
    assert parse_mirror_command("do the thing") is None
    assert parse_mirror_command("/mirror bogus") == {"action": "status"}


def test_links_conflict_replacement(tmp_path):
    link_slack(tmp_path, "conv-a", "C1")
    link_slack(tmp_path, "conv-b", "C1")
    assert conversation_for_slack(tmp_path, "C1") == "conv-b"
    assert slack_for_conversation(tmp_path, "conv-a") is None
    assert slack_for_conversation(tmp_path, "conv-b") == "C1"
    assert unlink_slack(tmp_path, "conv-b") is True
    assert conversation_for_slack(tmp_path, "C1") is None
    assert unlink_slack(tmp_path, "conv-b") is False


def test_whatsapp_mirror_lookups(tmp_path):
    link_slack(tmp_path, "conv-a", "C1")
    assert whatsapp_mirrored_for_conversation(tmp_path, "conv-a") is False
    set_whatsapp_mirror(tmp_path, "conv-a", True)
    assert whatsapp_mirrored_for_conversation(tmp_path, "conv-a") is True
    assert whatsapp_mirrored_for_slack(tmp_path, "C1") is True
    # Implicit slack row, set from Slack before any WebUI link.
    set_whatsapp_mirror(tmp_path, "slack:C9", True)
    assert whatsapp_mirrored_for_slack(tmp_path, "C9") is True
    assert waiter_keys_for_conversation(tmp_path, "conv-a", "1555") == [
        "slack:C1", "whatsapp:1555"]
    assert waiter_keys_for_slack(tmp_path, "C1", "1555") == [
        "conv-a", "whatsapp:1555"]
    assert "conv-a" in mirror_status_text(tmp_path, channel="C1")


def test_question_answered_from_linked_surface(tmp_path):
    server = WebUIServer(tmp_path)
    result = {}

    def _ask():
        result["answer"] = server.ask_question(
            "run1", "conv1", "which?", also_keys=("slack:C1",))

    thread = threading.Thread(target=_ask, daemon=True)
    thread.start()
    time.sleep(0.3)
    # Answering from the linked surface resolves the same waiter.
    assert server.answer_question("slack:C1", "from slack") == "run1"
    thread.join(timeout=5)
    assert result.get("answer") == "from slack"
    # The waiter is cleaned from every key.
    assert server.answer_question("conv1", "late") is None
    assert server.answer_question("slack:C1", "late") is None


def test_webui_mirror_command(tmp_path):
    server = WebUIServer(tmp_path)
    response = server.start_goal({"goal": "/mirror slack C9"})
    assert response["conversation_id"]
    assert slack_for_conversation(tmp_path, response["conversation_id"]) == "C9"
    response = server.start_goal({
        "goal": "/mirror whatsapp on",
        "conversation_id": response["conversation_id"]})
    assert whatsapp_mirrored_for_conversation(
        tmp_path, response["conversation_id"]) is True


def test_slack_mirror_command(tmp_path):
    server = WebUIServer(tmp_path)
    assert server._slack_mirror_command("C1", "/mirror webui conv9") is not None
    assert conversation_for_slack(tmp_path, "C1") == "conv9"
    server._slack_mirror_command("C1", "/mirror whatsapp on")
    assert whatsapp_mirrored_for_slack(tmp_path, "C1") is True


def test_practice_api(tmp_path):
    server = WebUIServer(tmp_path)
    assert server.handle_practice_list() == {"schedules": []}
    created = server.handle_practice_create({
        "name": "evening", "days": ["Mon"], "start": "21:00", "end": "22:00",
        "goals": [{"goal": "Write a haiku", "spec": ""}]})
    assert "schedule" in created
    assert len(server.handle_practice_list()["schedules"]) == 1
    assert server.handle_practice_create({
        "name": "evening", "days": [], "start": "21:00", "end": "22:00",
        "goals": [{"goal": "x", "spec": ""}]})["error"]
    # Run-now without the serve daemon reports honestly.
    assert "error" in server.handle_practice_run({"name": "evening"})
    assert server.handle_practice_run({"name": "missing"})["error"]
    assert server.handle_practice_delete({"name": "evening"}) == {"deleted": True}
    assert server.handle_practice_list() == {"schedules": []}
