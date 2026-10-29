"""Transcribe-stream /audio: a dead session's 410 must name its cause."""

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from src.interface.webui import WebUIServer


class _DeadSession:
    error = "server closed the session"

    def send_audio(self, pcm):
        return False


def test_audio_410_names_the_cause(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    server = WebUIServer(home)
    server.realtime_sessions["dead-sid"] = _DeadSession()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload = json.dumps({
            "session_id": "dead-sid",
            "audio_b64": base64.b64encode(b"\x00\x01" * 10).decode(),
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_address[1]}/api/transcribe_stream/audio",
            data=payload, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req)
            raise AssertionError("expected 410")
        except urllib.error.HTTPError as exc:
            assert exc.code == 410
            data = json.loads(exc.read().decode())
            assert data["error"] == "session closed: server closed the session"
    finally:
        httpd.shutdown()
        httpd.server_close()
