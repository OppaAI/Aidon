"""Natural-language asking, approval, and report styles.

Old Aidon wrote these messages as sentences, worded differently each
time. New Aidon used fixed templates ("Done: ...", "Rejected, no live
action taken."). The phrasing module varies the framing at zero model
cost; titles, card ids, approve/reject mechanics, and the stats line
are untouched. Asking on Slack/WhatsApp also works now: a waiting
question is delivered to the channel, and the next message there is
the answer, not a new goal.
"""

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.phrasing import (
    APPROVAL_LEAD_INS, QUESTION_ACKS, REPORT_DONE_OPENERS,
    approval_lead_in, files_sentence, question_ack, report_done_opener,
    report_text)
from src.interface.webui import WebUIServer
from src.system.main import _goal_extra_tools
from src.system.model_client import FakeClient


def test_report_text_done_keeps_stats_and_varies_by_run():
    text = report_text("done", ["about_me.pdf"], "", "run-1", "⏱️ 5s · 300 tokens")
    assert any(text.startswith(op) for op in REPORT_DONE_OPENERS)
    assert "about_me.pdf is ready." in text
    assert "⏱️ 5s · 300 tokens" in text
    openers = {report_done_opener(f"run-{i}") for i in range(30)}
    assert len(openers) > 1


def test_report_text_variants():
    assert report_text("failed", [], "boom", "r", "stats").startswith(
        ("That run failed:", "It didn't get there:", "Something went wrong:",
         "That one didn't make it:"))
    assert "boom" in report_text("failed", [], "boom", "r", "")
    assert report_text("rejected", [], "", "r", "") in (
        "Left as-is — no live action taken.",
        "OK, leaving it alone — nothing went live.",
        "Not going ahead with that — no live action taken.",
        "Understood — I won't take any live action on this.")
    assert files_sentence([]) == "Nothing was delivered this time."
    assert files_sentence(["a.pdf", "b.pdf"]) == "a.pdf and b.pdf are ready."


def test_approval_lead_ins_rotate_and_acks_vary():
    first = approval_lead_in()
    second = approval_lead_in()
    assert first != second
    assert first in APPROVAL_LEAD_INS and second in APPROVAL_LEAD_INS
    assert question_ack() in QUESTION_ACKS


def test_ask_for_approval_prepends_lead_in(tmp_path):
    seen = {}

    def decide(kind, title, body):
        seen["body"] = body
        return Decision("approved")

    gate = ApprovalGate(decide)
    runner = GoalRunner(FakeClient("hi"), Sandbox(tmp_path / "work"),
                        gate, tmp_path)
    runner._ask_for_approval("live", "Execute live", "Run the plan now.")
    assert seen["body"].startswith(tuple(APPROVAL_LEAD_INS))
    assert seen["body"].endswith("Run the plan now.")
    assert "Execute live" not in seen["body"]  # title is separate


def test_extra_tool_approval_prepends_lead_in(tmp_path):
    seen = {}

    def decide(kind, title, body):
        seen["body"] = body
        return Decision("approved")

    class _Config:
        home = tmp_path
        github_token = ""
        threads_account_id = ""
        threads_access_token = ""
        ig_user_id = ""
        ig_access_token = ""
        fish_api_key = ""
        tts_voice_id = ""
        transcribe_api_key = ""
        owner_email = "owner@example.com"

    tools = _goal_extra_tools(_Config(), tmp_path / "work",
                              gate=ApprovalGate(decide))
    tools["write_file"](name="x.txt", text="first")
    tools["write_file"](name="x.txt", text="second", overwrite=True)
    assert seen["body"].startswith(tuple(APPROVAL_LEAD_INS))
    assert "Replace the existing file" in seen["body"]


def _wait_for_waiter(server, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server.waiting_question(run_id) is not None:
            return True
        time.sleep(0.05)
    return False


def test_slack_answer_is_not_a_new_goal(tmp_path):
    sent = []
    server = WebUIServer(tmp_path,
                         slack_sender=lambda c, t, ts=None: sent.append((c, t)))
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(
            server.ask_question("run-9", "slack:C1", "Which plan?")),
        daemon=True)
    thread.start()
    assert _wait_for_waiter(server, "run-9")
    status, payload = server.handle_slack_post(
        b"", {},
        {"event": {"type": "message", "text": "Plan B",
                   "channel": "C1", "ts": "1.0"}})
    assert status == 200
    assert payload["replies"] and payload["replies"][0] in QUESTION_ACKS
    assert sent and sent[0][0] == "C1" and sent[0][1] in QUESTION_ACKS
    thread.join(timeout=5)
    assert answers == ["Plan B"]
    assert server.waiting_question("run-9") is None


def test_whatsapp_answer_is_not_a_new_goal(tmp_path):
    sent = []
    server = WebUIServer(tmp_path,
                         whatsapp_sender=lambda to, t: sent.append((to, t)))
    server.messaging_config.whatsapp_owner = "15551234567"
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(
            server.ask_question("run-10", "whatsapp:15551234567",
                                "Which contact?")),
        daemon=True)
    thread.start()
    assert _wait_for_waiter(server, "run-10")
    status, payload = server.handle_whatsapp_post(
        {"entry": [{"changes": [{"value": {"messages": [
            {"from": "15551234567",
             "text": {"body": "Email me"}}]}}]}]}, b"", {})
    assert status == 200
    assert payload["replies"] and payload["replies"][0] in QUESTION_ACKS
    assert sent and sent[0][1] in QUESTION_ACKS
    thread.join(timeout=5)
    assert answers == ["Email me"]
