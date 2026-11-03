"""Signed WhatsApp commands must reach Ask mode and its question channel."""

import hashlib
import hmac
import json
import threading
import time
from types import SimpleNamespace

import pytest

from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.interface.messaging import MessagingConfig
from src.interface.surfaces import mode_enabled
from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer
from src.system.model_client import FakeClient


def _server(home):
    dispatcher = TriggerDispatcher()
    server = WebUIServer(
        home, dispatcher=dispatcher,
        messaging_config=MessagingConfig(whatsapp_owner="16045551234",
                                         whatsapp_app_secret="test-signing-secret"),
        whatsapp_sender=lambda *_args: None)

    def send(text):
        payload = {"entry": [{"changes": [{"value": {"messages": [
            {"from": "16045551234", "text": {"body": text}}]}}]}]}
        raw = json.dumps(payload).encode()
        signature = hmac.new(b"test-signing-secret", raw, hashlib.sha256).hexdigest()
        return server.handle_whatsapp_post(
            payload, raw, {"x-hub-signature-256": "sha256=" + signature})

    return server, dispatcher, send


def _wait_for_question(server, run_id):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if server.waiting_question(run_id):
            return
        time.sleep(0.01)
    raise AssertionError("No interview question appeared")


@pytest.mark.parametrize("command", ["/ask", "/ask plan a new dinner"])
def test_mode_command_is_not_an_answer_to_an_existing_question(tmp_path, command):
    server, dispatcher, send = _server(tmp_path)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(server.ask_question(
        "old-run", "whatsapp:16045551234", "How many guests?")), daemon=True)
    worker.start()
    try:
        _wait_for_question(server, "old-run")
        status, reply = send(command)
        assert status == 200
        assert answers == []
        assert server.waiting_question("old-run") == "How many guests?"
        if command == "/ask":
            assert "Ask mode on" in reply["replies"][0]
            assert mode_enabled(tmp_path, "whatsapp:16045551234") == "ask"
        else:
            assert "interview is already waiting" in reply["replies"][0]
    finally:
        server.answer_question("whatsapp:16045551234", "six guests")
        worker.join(timeout=3)
    assert answers == ["six guests"]


@pytest.mark.parametrize("sticky", [True, False])
def test_signed_whatsapp_dinner_request_interviews_despite_chat_router(tmp_path, sticky):
    server, dispatcher, send = _server(tmp_path)
    reports = []
    routes = []

    class InterviewClient(FakeClient):
        def chat(self, messages, sampling=None):
            self.calls.append(messages)
            if len(self.calls) == 1:
                return json.dumps({"question": "How many guests?",
                                   "spec": {"goal": "Thanksgiving dinner"}})
            return json.dumps({"question": None, "complete": True,
                               "spec": {"goal": "Thanksgiving dinner",
                                        "deliverables": ["menu.md"],
                                        "budget_cents": 20000}})

    def handle(event):
        runner = GoalRunner(
            InterviewClient(), Sandbox(tmp_path / "work"),
            ApprovalGate(lambda *args: Decision("approved")), tmp_path,
            mode=event.payload["mode"], conversation_id=event.conversation_id,
            conversation_key=event.conversation_id,
            semantic_router=SimpleNamespace(classify=lambda *args, **kwargs: ("chat", 0.99)),
            use_semantic_router_skip=True,
            question_ask=lambda question: server.ask_question(
                "dinner-run", event.conversation_id, question))
        reports.append(runner.run(event.goal))
        routes.append(runner.last_route)

    dispatcher.register("whatsapp", handle)
    if sticky:
        assert "Ask mode on" in send("/ask")[1]["replies"][0]
    goal = "Give me ideas for Thanksgiving dinner, budget $200"
    worker = threading.Thread(target=send, args=(goal if sticky else "/ask " + goal,),
                              daemon=True)
    worker.start()
    try:
        _wait_for_question(server, "dinner-run")
        assert server.waiting_question("dinner-run") == "How many guests?"
        assert send("six guests")[0] == 200
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert reports[0].status == "stopped"
        assert reports[0].mode_stop["mode"] == "ask"
        assert routes == [("agentic", 1.0)]
    finally:
        server.answer_question("whatsapp:16045551234", "six guests")
        worker.join(timeout=3)
