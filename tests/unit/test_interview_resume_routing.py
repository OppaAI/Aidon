import threading

from src.interface.interview_store import InterviewStore
from src.interface.webui import WebUIServer


def test_resume_preserves_original_goal_and_routes_to_planning(tmp_path):
    class Dispatcher:
        def __init__(self):
            self.event = None
            self.received = threading.Event()
            self.handlers = {}
        def register(self, name, handler):
            self.handlers[name] = handler
        def dispatch(self, event):
            self.event = event
            self.received.set()

    dispatcher = Dispatcher()
    server = WebUIServer(tmp_path, dispatcher=dispatcher, memory=None)
    store = InterviewStore(tmp_path)
    store.save_session("session", {"goal": "I meant edit this video", "kind": "need_plugin",
                                  "conversation_id": "chat", "finished": False})
    result = server.start_goal({"goal": "/resume-interview", "conversation_id": "chat", "mode": "ask"})
    assert "run_id" in result
    assert dispatcher.received.wait(2)
    assert dispatcher.event.goal == "I meant edit this video"
    assert dispatcher.event.payload["mode"] == "plan"
    assert dispatcher.event.payload["resume_interview"] is True


def test_resuming_interview_bypasses_chat_route(tmp_path):
    from src.agentic.loop import GoalRunner
    from src.capabilities.sandbox import Sandbox
    from src.interface.approval import ApprovalGate
    from src.system.model_client import FakeClient

    class Router:
        def classify(self, goal, **kwargs):
            return ("chat", 1.0)

    runner = GoalRunner(FakeClient('{"status":"need_plugin"}'),
                        Sandbox(tmp_path / "work"), ApprovalGate(lambda *_: None), tmp_path,
                        conversation_id="chat", semantic_router=Router(),
                        use_semantic_router_skip=True, resume_interview=True,
                        mode="plan", question_ask=lambda question: "reject")
    assert runner.run("Make a video").status == "rejected"


def test_successful_run_record_is_conversation_scoped_skill_source(tmp_path):
    import json
    from src.interface.triggers import TriggerDispatcher

    server = WebUIServer(tmp_path, dispatcher=TriggerDispatcher(), memory=None)
    run_dir = tmp_path / "learned" / "runs"
    run_dir.mkdir(parents=True)
    record = {"status": "done", "run_id": "successful-123",
              "conversation_id": "chat-one", "goal": "Draft release notes",
              "deliverables": ["release.md"],
              "workflow": [{"id": "draft", "name": "Draft notes",
                            "tool": "write", "deliverable": "release.md",
                            "depends_on": [], "expected": "Verified facts"}]}
    (run_dir / "successful-123.json").write_text(json.dumps(record))

    source = server.handle_skill_source("successful-123", "chat-one")
    assert source and source["workflow"][0]["name"] == "Draft notes"
    assert server.handle_skill_source("successful-123", "other-chat") is None
    record["status"] = "failed"
    (run_dir / "successful-123.json").write_text(json.dumps(record))
    assert server.handle_skill_source("successful-123", "chat-one") is None
