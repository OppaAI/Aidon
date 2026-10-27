"""Integration wiring for the 'want all' build: redaction, webchat, extra tools, v1/MCP run, DAG merge, WebUI settings/replay."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.approval import ApprovalGate, Decision
from src.dag_store import DagStore
from src.embedder import HashEmbedder
from src.loop import GoalRunner
from src.memory import Memory
from src.model_client import FakeClient
from src.sandbox import Sandbox
from src.v1 import AuthStore, GoalStore, handle
from src.mcp_server import MCPServer
from src.webui import WebUIServer


def test_memory_redacts_at_record(tmp_path):
    memory = Memory(tmp_path, embedder=HashEmbedder())
    point = memory.record("working", "My key is Bearer abcdefgh12345678 and token=secret123456", {"note": "password=hunter2"})
    assert "abcdefgh12345678" not in point.text
    assert "secret123456" not in point.text
    assert "[REDACTED]" in point.text


def test_loop_webchat_and_extra_tool(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sandbox = Sandbox(home / "work")
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    # Webchat with a fake researcher
    class ChatRouter:
        def classify(self, goal):
            return ("webchat", 0.9)
    runner = GoalRunner(FakeClient("Webchat answer"), sandbox, gate, home,
                        semantic_router=ChatRouter(), use_semantic_router_skip=True,
                        researcher=lambda goal: ["Fact: Vancouver is in BC."])
    report = runner.run("What is the weather in Vancouver?")
    assert report.status == "chat"
    assert "Webchat answer" in (report.chat_reply or "")
    # Extra tool dispatch
    runner2 = GoalRunner(FakeClient(""), sandbox, gate, home, extra_tools={"echo_tool": lambda text: f"echo:{text}"})
    assert runner2._call_tool("echo_tool", {"text": "hi"}) == "echo:hi"


def test_v1_and_mcp_run_goal(tmp_path):
    auth = AuthStore(tmp_path)
    key, _ = auth.create_key("owner1", "owner")
    goals = GoalStore(tmp_path)
    goal = goals.submit("Do a thing", "default", "owner1")
    headers = {"Authorization": key}
    status, payload = handle(auth, goals, "POST", f"/api/v1/goals/{goal.id}/run", headers, {},
                             execute=lambda g: {"status": "done", "files": ["out.md"]})
    assert status == 200
    assert payload["goal"]["status"] == "done"
    # MCP run_goal, owner only
    server = MCPServer(auth, goals, execute=lambda g: {"status": "done"})
    result = server.handle({"method": "tools/call", "id": 1, "params": {"name": "run_goal", "arguments": {"id": goal.id}}}, key)
    assert "error" not in result


def test_dag_merge_and_webui_settings_replay(tmp_path):
    store = DagStore(tmp_path)
    ok, _ = store.save("d1", "Goal", [{"id": "a", "tool": "write", "args": {"file": "a.md", "text": "A"}, "deliverable": "a.md"}])
    assert ok
    ok, _ = store.save("d1", "Goal", [{"id": "b", "tool": "write", "args": {"file": "b.md", "text": "B"}, "deliverable": "b.md"}], merge=True)
    assert ok
    data = store.get("d1")
    ids = {n["id"] for n in data["nodes"]}
    assert ids == {"a", "b"}
    # WebUI settings allowlist
    server = WebUIServer(tmp_path)
    assert server.settings_set("timezone", "America/Vancouver") is True
    assert server.settings_set("AIDON_API_KEY", "secret") is False
    assert server.settings_get()["timezone"] == "America/Vancouver"
    # Replay runs list, empty trace dir
    assert server.replay_runs() == []
    # Auth helper via server token: localhost without token allowed, remote denied is tested in subagent file, here just check token stored
    assert server.webui_token is None
