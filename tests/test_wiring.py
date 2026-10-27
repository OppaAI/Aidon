"""Wiring tests: Gmail, MCP client, pre-approval, memory extras, research, transcript, factory, polling."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.gmail import gmail_sender, refresh_access_token
from src.mcp_client import McpClient
from src.memory_extras import backup, export_points, redact, rolling_summary
from src.preapproval import PreapprovalStore
from src.research_allow import is_allowed, load_allowlist, research_multiround
from src.youtube_transcript import fetch_transcript
from src.messaging import poll_slack_once, MessagingConfig
from src.webui import WebUIServer
from src.triggers import TriggerDispatcher


def test_gmail_sender_fake(tmp_path):
    def fake_post(url, payload):
        return {"access_token": "tok123"}
    def fake_send(token, payload):
        assert token == "tok123"
        return {"id": "msg1"}
    sender = gmail_sender("cid", "csec", "r", post=fake_post, send=fake_send)
    assert sender("a@example.com", "Hi", "Body") == {"id": "msg1"}


def test_preapproval_refuses_external_and_irreversible(tmp_path):
    store = PreapprovalStore(tmp_path)
    assert store.grant("spec1", "send_external", 9999999999) is False
    assert store.grant("spec1", "delete", 9999999999) is False
    assert store.grant("spec1", "sandbox", 9999999999) is True
    assert store.is_preapproved("spec1", "sandbox", now=1000) is True
    assert store.is_preapproved("spec1", "send_external", now=1000) is False


def test_redact_and_rolling_summary(tmp_path):
    home = tmp_path
    path = rolling_summary(home, "dinner", "Email me at a@example.com about dinner", ["menu.pdf"], "done")
    assert "a@example.com" not in path.read_text()
    assert "menu.pdf" in path.read_text()


def test_backup_excludes_secrets(tmp_path):
    home = tmp_path / "home"
    (home / "specs").mkdir(parents=True)
    (home / "specs" / "a.md").write_text("spec")
    (home / ".env").write_text("AIDON_API_KEY=secret")
    copied = backup(home, tmp_path / "backup")
    assert "specs/a.md" in copied
    assert not (tmp_path / "backup" / ".env").exists()


def test_research_allowlist_and_multiround():
    assert is_allowed("https://www.save-on-foods.com/x", ["save-on-foods.com"]) is True
    assert is_allowed("https://other.com/x", ["save-on-foods.com"]) is False
    assert is_allowed("https://other.com/x", []) is True
    class R:
        def __init__(self):
            self.calls = 0
        def __call__(self, query):
            self.calls += 1
            return {"facts": []} if self.calls == 1 else {"facts": [{"url": "https://a.com"}]}
    researcher = R()
    result = research_multiround("q", researcher, rounds=2)
    assert researcher.calls == 2
    assert result["facts"]


def test_youtube_transcript_fake():
    xml = '<transcript><text start="0">Hello</text><text start="1">world</text></transcript>'
    assert fetch_transcript("vid", fetch=lambda url: xml) == "Hello world"
    assert fetch_transcript("vid", fetch=lambda url: "") == ""


def test_slack_polling_skips_bots_and_old():
    config = MessagingConfig(slack_bot_token="x")
    def fake_fetch(url, token, params):
        return {"messages": [{"ts": "3", "text": "new"}, {"ts": "2", "text": "bot", "bot_id": "B1"}, {"ts": "1", "text": "old"}]}
    messages, newest = poll_slack_once(config, "C1", last_ts="1", fetch=fake_fetch)
    assert [m["text"] for m in messages] == ["new"]
    assert newest == "3"


def test_factory_stages_from_trace(tmp_path):
    home = tmp_path
    (home / "trace").mkdir()
    (home / "trace" / "run1.jsonl").write_text(json.dumps({"kind": "plan", "status": "done"}) + "\n")
    server = WebUIServer(home, dispatcher=TriggerDispatcher())
    stages = server.factory_stages()
    assert stages and stages[0]["kind"] == "plan"


def test_mcp_client_fake_server(tmp_path):
    script = tmp_path / "fake_mcp.py"
    script.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    req = json.loads(line)\n"
        "    method = req.get('method')\n"
        "    result = {'tools': [{'name': 'echo'}]} if method == 'tools/list' else {}\n"
        "    print(json.dumps({'jsonrpc': '2.0', 'id': req.get('id'), 'result': result}), flush=True)\n")
    client = McpClient(["python3", str(script)])
    client.start()
    assert client.list_tools() == [{"name": "echo"}]
    client.close()
