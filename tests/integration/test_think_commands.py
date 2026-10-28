"""Integration tests for /think + /help on the WebUI surface.

A recording dispatcher captures the TriggerEvent start_goal
dispatches, so the tests prove the think flag reaches the run
payload without running a model.
"""

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer


def _post(base, path, payload):
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def _get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return resp.status, json.loads(resp.read())


def test_think_toggle_help_and_once_flow(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    events = []
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", events.append)
    server = WebUIServer(home, dispatcher=dispatcher, memory=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        # Button toggle endpoint flips the per-conversation flag.
        _, toggled = _post(base, "/api/think", {"conversation_id": "c1"})
        assert toggled["think_max"] is True
        _, state = _get(base, "/api/think?conversation_id=c1")
        assert state["think_max"] is True
        _, toggled = _post(base, "/api/think", {"conversation_id": "c1"})
        assert toggled["think_max"] is False

        # Typed bare /think toggles and answers without a run.
        _, reply = _post(base, "/api/goals",
                         {"goal": "/think", "conversation_id": "c1"})
        record = server._web_runs[reply["run_id"]]
        assert record["status"] == "done"
        assert "Maximum thinking on" in record["events"][0]["text"]
        assert len(events) == 0  # no goal dispatched

        # A goal after the toggle inherits the flag.
        _, run = _post(base, "/api/goals",
                       {"goal": "hello", "conversation_id": "c1"})
        assert len(events) == 1
        assert events[0].payload["think_max"] is True
        assert events[0].goal == "hello"

        # /think <goal> runs once with max thinking, flag unchanged.
        _, run = _post(base, "/api/goals",
                       {"goal": "/think build a shed", "conversation_id": "c2"})
        assert len(events) == 2
        assert events[1].payload["think_max"] is True
        assert events[1].goal == "build a shed"
        _, state = _get(base, "/api/think?conversation_id=c2")
        assert state["think_max"] is False

        # /help answers with the command list, no run dispatched.
        _, reply = _post(base, "/api/goals",
                         {"goal": "/help", "conversation_id": "c1"})
        record = server._web_runs[reply["run_id"]]
        assert record["status"] == "done"
        assert "/think" in record["events"][0]["text"]
        assert "/mirror" in record["events"][0]["text"]
        assert len(events) == 2
    finally:
        httpd.shutdown()
        httpd.server_close()
