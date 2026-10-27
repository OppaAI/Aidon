"""Real research, live execution, live verification, incremental improving.

Research is tested against a real local HTTP server, not
a fake string. Live actions are a closed list, with
snapshot rollback on live verification failure.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from src.interface.approval import ApprovalGate, Decision
from src.capabilities.live import LiveExecutor
from src.agentic.loop import GoalRunner
from src.system.model_client import FakeClient
from src.agentic.research import Researcher
from src.capabilities.sandbox import Sandbox


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><p>Roast beef is the main dish.</p></body></html>")

    def log_message(self, *args):
        pass


def _server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_research_verifies_only_sourced_facts():
    server = _server()
    try:
        url = f"http://127.0.0.1:{server.server_port}/"
        report = Researcher().research(
            "What is the main dish?", [url],
            claims=["Roast beef is the main dish.", "Turkey is the main dish."])
        assert len(report.verified_facts) == 1
        assert report.verified_facts[0]["fact"] == "Roast beef is the main dish."
        assert report.verified_facts[0]["source"] == url
        assert report.unverified_claims == ["Turkey is the main dish."]
    finally:
        server.shutdown()


def test_live_copy_and_rollback(tmp_path):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    (work / "note.md").write_text("done", encoding="utf-8")
    output = tmp_path / "out" / "note.md"
    executor = LiveExecutor(work, home)
    record = executor.execute([{"kind": "copy_to_output", "file": "note.md", "output": str(output)}])
    assert executor.verify_live(record) is True
    assert output.read_text() == "done"
    # Overwrite with a bad live run, then rollback to the good file.
    (work / "note.md").write_text("bad", encoding="utf-8")
    executor2 = LiveExecutor(work, home)
    record2 = executor2.execute([{"kind": "copy_to_output", "file": "note.md", "output": str(output)}])
    assert output.read_text() == "bad"
    executor2.rollback(record2)
    assert output.read_text() == "done"


def test_live_action_not_allowed(tmp_path):
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True)
    executor = LiveExecutor(work, home)
    record = executor.execute([{"kind": "shell", "command": ["echo", "hi"]}])
    assert record.error.startswith("live action not allowed")


def test_loop_live_and_incremental_lesson(tmp_path):
    home = tmp_path / "home"
    plan = {"steps": [{"tool": "write", "args": {"file": "note.md", "text": "done"}, "deliverable": "note.md"}],
            "deliverables": ["note.md"], "criteria": []}
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"))
    output = tmp_path / "out" / "note.md"
    runner = GoalRunner(FakeClient(json.dumps(plan)), Sandbox(home / "work"),
                        gate, home, memory=None)
    report = runner.run("Write a note", live_actions=[
        {"kind": "copy_to_output", "file": "note.md", "output": str(output)}])
    assert report.status == "done"
    assert output.read_text() == "done"
    learned = json.loads((home / "learned" / "last_run.json").read_text())
    assert learned["live"]["verified"] is True
    assert learned["live"]["outputs"] == [str(output)]
