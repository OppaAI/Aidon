from src.system.main import _goal_extra_tools
from src.agentic.loop import GoalRunner, Plan, RunReport
from src.system.model_client import FakeClient
from src.capabilities.sandbox import Sandbox
from src.interface.approval import ApprovalGate, Decision
from src.agentic.think_effort import sampling_for
from src.capabilities.tools import ToolRegistry
from src.memory.docstore import _sparse_vector


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


def test_sampling_reaches_client(tmp_path):
    client = FakeClient('{"steps": [], "deliverables": [], "criteria": []}')
    runner = GoalRunner(client, Sandbox(tmp_path), ApprovalGate(lambda kind, title, body: Decision('approved', 'test')), tmp_path)
    runner._chat("verify", [])
    assert client.last_sampling == sampling_for("verify")


def test_versioned_write_tool(tmp_path):
    registry = ToolRegistry(tmp_path, versioned_writes=True)
    assert "version 1" in registry.call("write", {"file": "a.md", "text": "one"})
    assert "version 2" in registry.call("write", {"file": "a.md", "text": "two"})
    try:
        registry.call("write", {"file": "a.md", "text": "three", "expected_hash": "deadbeef"})
        raise AssertionError("conflict should raise")
    except ValueError as exc:
        assert "conflict" in str(exc)


def test_report_carries_step_lines_and_delivery(tmp_path):
    client = FakeClient('{"steps": [{"tool": "write", "args": {"file": "a.md", "text": "hi"}, "deliverable": "a.md"}], "deliverables": ["a.md"], "criteria": []}')
    runner = GoalRunner(client, Sandbox(tmp_path), ApprovalGate(lambda kind, title, body: Decision('approved', 'test')), tmp_path)
    report = runner.run("write a.md")
    assert report.step_lines, "step lines should be recorded"
    assert "a.md" in report.delivery_summary


def test_plan_guard_cycle_fails(tmp_path):
    client = FakeClient('{"steps": [], "deliverables": [], "criteria": []}')
    runner = GoalRunner(client, Sandbox(tmp_path), ApprovalGate(lambda kind, title, body: Decision('approved', 'test')), tmp_path)
    plan = Plan(steps=[{"tool": "write", "args": {"file": "a.md"}, "deliverable": "a.md", "depends_on": ["step-1"]},
                      {"tool": "write", "args": {"file": "b.md"}, "deliverable": "b.md", "depends_on": ["step-0"]}],
                deliverables=["a.md", "b.md"], criteria=[])
    report = runner._execute_plan("cycle goal", plan)
    assert report.status == "failed" and "cycle" in report.error


def test_sparse_vector_and_conversations_db(tmp_path):
    indices, values = _sparse_vector("hello world hello")
    assert indices and values and len(indices) == len(values)
    from src.memory.conversations_db import ConversationStoreDB
    db = ConversationStoreDB(tmp_path)
    conv = db.create_conversation("proj-a")
    db.record_exchange(conv["id"], "user", "hi")
    assert db.list_all_conversations()
    assert db.import_conversation("legacy-1", "proj-a", [{"role": "user", "text": "old"}])["id"] == "legacy-1"
    assert db.history("legacy-1")[0]["text"] == "old"


def test_webui_conversations_use_sqlite(tmp_path):
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path, memory=None)
    conv = server.handle_conversation_create({"project_id": "proj-a", "turns": [{"role": "user", "text": "hi"}]})
    assert conv["project_id"] == "proj-a"
    assert any(c["id"] == conv["id"] for c in server.handle_conversations_list()["conversations"])
    got = server.handle_conversation_get(conv["id"])
    assert got["id"] == conv["id"] and got["turns"][0]["text"] == "hi"
    assert server.handle_conversation_delete(conv["id"])["ok"] is True
    assert server.handle_conversation_get(conv["id"]) is None
    assert (tmp_path / "conversations.db").exists()


def test_settings_env_model_keys(tmp_path):
    from src.interface.webui import WebUIServer
    server = WebUIServer(tmp_path, memory=None)
    assert server.settings_set_env("model", "muse-spark-1.3")["ok"] is True
    assert server.settings_set_env("fast_model", "muse-spark-1.3")["ok"] is True
    assert server.settings_set_env("tts_voice_id", "v1")["ok"] is True
    assert server.settings_set_env("AIDON_API_KEY", "x")["ok"] is False
