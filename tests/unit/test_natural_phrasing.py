"""Natural-language asking, approval, and report styles.

Asking, approval, and report messages read like sentences, worded
differently each time, while the run's own messages use fixed
templates ("Done: ...", "Rejected, no live
action taken."). The phrasing module varies the framing at zero model
cost; titles, card ids, approve/reject mechanics, and the stats line
are untouched. Asking on Slack/WhatsApp also works now: a waiting
question is delivered to the channel, and the next message there is
the answer, not a new goal.
"""

import hashlib
import hmac
import json
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


def test_report_text_includes_result_without_file_deliverable():
    text = report_text("done", [], "", "run-output", "stats",
                       result_text="/workspace/run-123")
    assert "The requested action completed." in text
    assert "Tool result:" in text
    assert "/workspace/run-123" in text
    assert "Nothing was delivered" not in text


def test_report_text_renders_markdown_readback_as_markdown():
    text = report_text(
        "done", ["grocery-plan.md"], "", "run-markdown", "stats",
        result_text="read_file:\n# Grocery plan\n\n## Meal Plan\n- Oatmeal")
    assert "**read_file:**" in text
    assert "# Grocery plan" in text
    assert "## Meal Plan" in text
    assert "```text" not in text


def test_report_text_renders_markdown_after_descriptive_tool_label():
    text = report_text(
        "done", ["grocery-plan.md"], "", "run-described-readback", "stats",
        result_text=("Read current grocery-plan.md before editing:\n"
                     "# Grocery plan\n\n## Shopping List\n- Zucchini"))
    assert "**Read current grocery-plan.md before editing:**" in text
    assert "# Grocery plan" in text
    assert "```text" not in text


def test_report_text_keeps_plain_readback_in_code_block():
    text = report_text("done", [], "", "run-plain", "stats",
                       result_text="read_file:\nplain output")
    assert "```text\nread_file:" in text


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
    # No model phrasing here: the model call fails, so the rotation
    # lead-in is used (the model-phrase path has its own test).
    runner._chat = lambda phase, messages: (_ for _ in ()).throw(
        RuntimeError("no model"))
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


def _sign(secret, raw):
    """Valid Slack HMAC headers for raw, as production sends them."""
    import hashlib
    import hmac
    import time

    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + raw,
                            hashlib.sha256).hexdigest()
    return {"x-slack-request-timestamp": ts, "x-slack-signature": sig}


def test_slack_answer_is_not_a_new_goal(tmp_path):
    sent = []
    server = WebUIServer(tmp_path,
                         slack_sender=lambda c, t, ts=None: sent.append((c, t)))
    server.slack_signing_secret = "secret123"
    answers = []
    thread = threading.Thread(
        target=lambda: answers.append(
            server.ask_question("run-9", "slack:C1", "Which plan?")),
        daemon=True)
    thread.start()
    assert _wait_for_waiter(server, "run-9")
    body = {"event": {"type": "message", "text": "Plan B",
                   "channel": "C1", "ts": "1.0"}}
    raw = json.dumps(body).encode()
    status, payload = server.handle_slack_post(raw, _sign("secret123", raw), body)
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


# --- fast-model phrasing (wired in the next change) ---

from src.interface.phrasing import phrase_with_model
from src.system.config import Config


def test_phrase_with_model_cleans_and_falls_back():
    assert phrase_with_model("approval", lambda p: '  "One quick check:"\nSecond line\n') == "One quick check:"
    assert phrase_with_model("approval", lambda p: (_ for _ in ()).throw(RuntimeError("down"))) is None
    assert phrase_with_model("approval", lambda p: "   ") is None
    assert phrase_with_model("approval", lambda p: "x" * 200) is None
    assert phrase_with_model("unknown-kind", lambda p: "hi") is None


def test_phrase_with_model_timeout_does_not_hang():
    import time

    def slow(prompt):
        time.sleep(2)
        return "too late"

    started = time.time()
    assert phrase_with_model("approval", slow, timeout_s=0.05) is None
    assert time.time() - started < 1.0


def test_report_text_phrase_override():
    text = report_text("done", ["a.pdf"], "", "r", "stats",
                       phrase="Freshly finished:")
    assert text.startswith("Freshly finished: a.pdf is ready.")
    assert text.endswith("stats")
    text = report_text("rejected", [], "", "r", "",
                       phrase="Leaving that one alone.")
    assert text == "Leaving that one alone."


def test_ask_for_approval_uses_model_phrase_then_falls_back(tmp_path):
    seen = {}

    def decide(kind, title, body):
        seen["body"] = body
        return Decision("approved")

    gate = ApprovalGate(decide)
    runner = GoalRunner(FakeClient("hi"), Sandbox(tmp_path / "work"),
                        gate, tmp_path)
    runner._chat = lambda phase, messages: "A quick nod before we go:"
    runner._ask_for_approval("live", "Execute live", "Run the plan now.")
    assert seen["body"].startswith("A quick nod before we go:")
    assert seen["body"].endswith("Run the plan now.")

    def failing_chat(phase, messages):
        raise RuntimeError("no model")

    runner._chat = failing_chat
    runner._ask_for_approval("live", "Execute live", "Run the plan now.")
    assert seen["body"].startswith(tuple(APPROVAL_LEAD_INS))
    assert seen["body"].endswith("Run the plan now.")


def test_extra_tool_approval_uses_phrase_param(tmp_path):
    seen = {}

    def decide(kind, title, body):
        seen["body"] = body
        return Decision("approved")

    class _Config2:
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

    tools = _goal_extra_tools(_Config2(), tmp_path / "work",
                              gate=ApprovalGate(decide),
                              phrase=lambda kind: "Please give the nod:")
    tools["write_file"](name="y.txt", text="first")
    tools["write_file"](name="y.txt", text="second", overwrite=True)
    assert seen["body"].startswith("Please give the nod:")
    assert "Replace the existing file" in seen["body"]


def test_config_fast_endpoint_and_key(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDON_FAST_MODEL", "llama-3.1-8b-instruct")
    monkeypatch.setenv("AIDON_FAST_API_ENDPOINT", "http://localhost:11434/v1")
    monkeypatch.setenv("AIDON_FAST_API_KEY", "local-key")
    cfg = Config.load(home=tmp_path)
    assert cfg.fast_model == "llama-3.1-8b-instruct"
    assert cfg.fast_api_endpoint == "http://localhost:11434/v1"
    assert cfg.fast_api_key == "local-key"
