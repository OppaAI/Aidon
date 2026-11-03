"""Intent routing in every mode, and no live approval without live actions.

A chat- or webchat-routed message gets a direct reply even when the
chooser is on Plan. Ask mode always interviews, even when the router
classifies a dinner idea request as chat or webchat. And a run
whose plan has no live actions never asks "Approve live execution?",
a gate whose force_ask would otherwise repeat the mode-stop approval
for an empty set (and bypass even an Auto-Approve grant).
"""

import json
import pytest
from types import SimpleNamespace

from src.interface.approval import ApprovalGate, Decision
from src.agentic.loop import GoalRunner
from src.capabilities.sandbox import Sandbox
from src.system.model_client import FakeClient


def _runner(tmp_path, client, **kwargs):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    return GoalRunner(client, Sandbox(home / "work"), gate, home,
                      memory=None, **kwargs)


class _Route:
    def __init__(self, route, confidence=0.9):
        self.route = route
        self.confidence = confidence

    def classify(self, goal, judge=None):
        return (self.route, self.confidence)


def test_plan_mode_chat_route_skips_interview(tmp_path):
    def _never_ask(question):
        raise AssertionError("interview must not run for a chat-routed greeting")

    runner = _runner(tmp_path, FakeClient("hey, doing well"),
                     semantic_router=_Route("chat"),
                     use_semantic_router_skip=True,
                     mode="plan", question_ask=_never_ask,
                     conversation_id="greeting")
    report = runner.run("Hey Aidon, how are you doing today?")
    assert report.status == "chat"
    assert report.chat_reply
    assert not report.mode_stop


def test_plain_chat_route_includes_recalled_cross_chat_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDON_MEMORY_EXTRACT", "0")

    class _Memory:
        def recall_both(self, query, episode_limit, knowledge_limit):
            return [], []

        def recall(self, collection, query, limit=5):
            assert collection == "semantic"
            assert "test project" in query
            if "name" not in query:
                return []
            point = SimpleNamespace(
                text="The user's test project is named Cedar Lantern.",
                payload={})
            return [SimpleNamespace(point=point)]

        def record_episodic(self, *args, **kwargs):
            return object()

        def promote_to_ltm(self, *args, **kwargs):
            return None

    home = tmp_path / "home"
    home.mkdir()
    client = FakeClient("Your test project is Cedar Lantern.")
    runner = GoalRunner(
        client, Sandbox(home / "work"),
        ApprovalGate(lambda kind, title, body: Decision("approved")), home,
        memory=_Memory(), semantic_router=_Route("chat"),
        use_semantic_router_skip=True, conversation_id="new-chat",
        guidance="Keep the answer concise.")

    report = runner.run("What was my test project called?")

    assert report.status == "chat"
    assert "Cedar Lantern" in report.chat_reply
    sent = "\n".join(message.content for call in client.calls
                      for message in call)
    assert "Cedar Lantern" in sent
    assert "Keep the answer concise." in sent
    assert "Do not reinterpret a word from the question" in sent


def test_plan_mode_webchat_route_skips_interview(tmp_path):
    def _never_ask(question):
        raise AssertionError("interview must not run for a webchat-routed question")

    runner = _runner(tmp_path, FakeClient("sunny and clear"),
                     semantic_router=_Route("webchat"),
                     use_semantic_router_skip=True,
                     mode="plan", question_ask=_never_ask,
                     researcher=lambda goal: "Vancouver weather: sunny",
                     conversation_id="weather")
    report = runner.run("What is the weather in Vancouver today?")
    assert report.status == "chat"
    assert report.chat_reply
    assert not report.mode_stop


@pytest.mark.parametrize("route", ["agentic", "chat", "webchat"])
def test_ask_mode_always_interviews(tmp_path, route):
    class _InterviewClient(FakeClient):
        def __init__(self):
            self.calls = 0

        def chat(self, messages, sampling=None):
            self.calls += 1
            if self.calls == 1:
                return json.dumps({"question": "What is the main dish?",
                                   "spec": {"goal": "Plan a dinner",
                                            "deliverables": []}})
            return json.dumps({"question": None, "complete": True,
                               "spec": {"goal": "Plan a dinner",
                                        "deliverables": ["menu.pdf"],
                                        "constraints": ["no milk"],
                                        "budget_cents": 10000}})

    runner = _runner(tmp_path, _InterviewClient(),
                     semantic_router=_Route(route, 1.0),
                     use_semantic_router_skip=True,
                     mode="ask", question_ask=lambda q: "roast beef",
                     conversation_id="dinner")
    report = runner.run("Give me ideas for Thanksgiving dinner with a $200 budget")
    assert runner.last_route == ("agentic", 1.0)
    assert report.status == "stopped"
    assert report.mode_stop
    assert report.mode_stop["mode"] == "ask"


def test_live_approval_skipped_when_no_live_actions(tmp_path):
    plan = {"steps": [{"tool": "write",
                        "args": {"file": "out.md", "text": "done"},
                        "deliverable": "out.md"}],
            "deliverables": ["out.md"],
            "criteria": ["out.md contains done"]}
    asked = []

    def _ask(kind, title, body, **kwargs):
        asked.append((kind, title))
        return Decision("approved")

    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    gate = ApprovalGate(_ask)
    runner = GoalRunner(FakeClient(json.dumps(plan)),
                        Sandbox(home / "work"), gate, home, memory=None)
    report = runner.run("Write out")
    assert report.status == "done"
    assert ("live", "Approve live execution?") not in asked


def test_desktop_control_cannot_be_swallowed_by_chat_route(tmp_path):
    runner = _runner(tmp_path, FakeClient("I cannot open apps"),
                     semantic_router=_Route("chat"),
                     use_semantic_router_skip=True, mode="plan",
                     extra_tools={"computer_use_task": lambda **kwargs: "unused"})
    report = runner.run("Open gedit on my Ubuntu desktop")
    assert report.status == "stopped"
    assert runner.last_route == ("agentic", 1.0)
    assert report.plan.normalized_steps()[0].tool == "computer_use_task"


@pytest.mark.parametrize("kernel_mode,tools", [(False, {}), (True, {"computer_use_task": object()})])
def test_unavailable_desktop_never_fabricates_execution_in_chat(tmp_path, kernel_mode, tools):
    client = FakeClient('[Visible gedit window opens, shows the typed message]')
    runner = _runner(tmp_path, client, semantic_router=_Route("chat"),
                     use_semantic_router_skip=True, mode="plan",
                     kernel_mode=kernel_mode, extra_tools=tools)
    report = runner.run(
        'On the Ubuntu desktop, visibly open gedit and type "Hello from Aidon." '
        'Show me each desktop action and ask for approval before clicking or typing. '
        'Afterward, check the screen and tell me whether the text appeared.')
    assert report.status == "failed"
    assert "No desktop action was performed" in report.error
    assert not report.chat_reply
    assert runner.last_route == ("agentic", 1.0)
    assert client.calls == []


def test_chat_followup_does_not_treat_prose_as_tool_evidence(tmp_path):
    client = FakeClient("No desktop action was executed.")
    runner = _runner(tmp_path, client, semantic_router=_Route("chat"),
                     use_semantic_router_skip=True,
                     conversation_context='Assistant: [Visible gedit window opens]')
    report = runner.run("Yes")
    assert report.status == "chat"
    assert "no tools have been executed in this turn" in str(client.calls)
    assert "not tool evidence" in str(client.calls)
