"""Integration wiring for the sixth-audit batch, done by the parent:
_network_available in main.py and Slack image markers in the WebUI
Slack handler. These are the two cross-agent seams no single part
owned, so they get their own tests here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from src.system.main import _network_available


class _Docker:
    def __init__(self, allow):
        self.allow_network = allow


class _Bwrap:
    pass


class _Sandbox:
    def __init__(self, bwrap):
        self.bwrap = bwrap


def test_network_available_docker_flag():
    assert _network_available(_Docker(True), _Sandbox(True)) is True
    assert _network_available(_Docker(False), _Sandbox(False)) is False


def test_network_available_bwrap_backend_is_offline():
    assert _network_available(_Bwrap(), _Sandbox(True)) is False


def test_network_available_no_backend_follows_sandbox():
    assert _network_available(None, _Sandbox(True)) is False
    assert _network_available(None, _Sandbox(None)) is True


def test_slack_handler_attaches_image_markers(tmp_path):
    from src.interface.webui import WebUIServer

    seen = []

    class _Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def dispatch(self, event):
            seen.append(event)

    server = WebUIServer(tmp_path, dispatcher=_Dispatcher())
    body = {"event": {"type": "message", "text": "what is this",
                      "channel": "C1", "user": "U1",
                      "files": [{"id": "F1", "mimetype": "image/png",
                                 "name": "photo.png", "filetype": "png"}]}}
    status, _payload = server.handle_slack_post(b"", {}, body)
    assert status == 200
    assert len(seen) == 1
    assert "[image slack_file_id=F1" in seen[0].goal
    assert seen[0].payload["images"][0]["id"] == "F1"


def test_slack_handler_plain_text_unchanged(tmp_path):
    from src.interface.webui import WebUIServer

    seen = []

    class _Dispatcher:
        def __init__(self):
            self.handlers = {}

        def register(self, name, fn):
            self.handlers[name] = fn

        def dispatch(self, event):
            seen.append(event)

    server = WebUIServer(tmp_path, dispatcher=_Dispatcher())
    body = {"event": {"type": "message", "text": "hello",
                      "channel": "C1", "user": "U1"}}
    status, _payload = server.handle_slack_post(b"", {}, body)
    assert status == 200
    assert seen[0].goal == "hello"
    assert seen[0].payload == {}
