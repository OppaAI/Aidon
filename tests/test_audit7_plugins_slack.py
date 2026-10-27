"""Seventh-audit part C: adopt callback hash binding + Slack download.

The gate.ask adopt path already bound approval to a tree hash; the
approve-callback path did not show the approver what it approved.
And Slack file bytes had no download helper at all (markers +
metadata only). Both are covered here, without wiring the helper
into any handler.
"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from src.capabilities.plugins import PluginRegistry, plugin_tree_hash
from src.interface.messaging import download_slack_file


def _plugin_dir(tmp_path, name="upper"):
    directory = tmp_path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.json").write_text(json.dumps({
        "name": name, "version": "1.0.0",
        "description": "Test plugin",
        "tools": [{"name": "upper", "description": "Uppercase text",
                   "module": "tool.py", "function": "upper"}],
        "skills": []}), encoding="utf-8")
    (directory / "tool.py").write_text(
        "def upper(args):\n    return str(args.get('text', '')).upper()\n",
        encoding="utf-8")
    return directory


def test_callback_receives_hash_and_body(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    seen = {}

    def approve(plugin, findings, tree_hash, body):
        seen["hash"] = tree_hash
        seen["body"] = body
        seen["name"] = plugin.name
        return True

    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(directory, approve=approve)
    assert ok is True
    assert seen["hash"] == plugin_tree_hash(directory)
    assert seen["name"] == "upper"
    assert f"Approved source hash: {seen['hash']}" in seen["body"]
    assert "Plugin: upper 1.0.0" in seen["body"]


def test_callback_old_arity_still_works(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(
        directory, approve=lambda plugin, findings: True)
    assert ok is True
    assert (home / "plugins" / "upper" / "tool.py").is_file()


def test_callback_rejection_does_not_copy(tmp_path):
    home = tmp_path / "home"
    directory = _plugin_dir(tmp_path)
    registry = PluginRegistry(home)
    ok, _findings, _message = registry.adopt(
        directory, approve=lambda plugin, findings, h, b: False)
    assert ok is False
    assert not (home / "plugins" / "upper").exists()


def test_download_private_url_via_fake_get():
    calls = []

    def fake_get(url, token):
        calls.append((url, token))
        return b"PNGDATA"

    data = download_slack_file(
        "xoxb-test-token", "https://files.slack.com/files-pri/T1/F1/photo.png",
        get=fake_get)
    assert data == b"PNGDATA"
    assert calls == [("https://files.slack.com/files-pri/T1/F1/photo.png",
                      "xoxb-test-token")]
    assert "xoxb-test-token" not in calls[0][0]


def test_download_file_id_resolves_via_files_info():
    calls = []

    def fake_get(url, token):
        calls.append(url)
        if "files.info" in url:
            assert "token=" not in url
            return {"ok": True,
                    "file": {"url_private": "https://files.slack.com/files-pri/T1/F1/photo.png"}}
        return b"BYTES"

    data = download_slack_file("xoxb-test-token", "F1ABC", get=fake_get)
    assert data == b"BYTES"
    assert calls[0].startswith("https://slack.com/api/files.info?file=F1ABC")
    assert calls[1] == "https://files.slack.com/files-pri/T1/F1/photo.png"


def test_download_refuses_non_https_before_any_request():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"SHOULD-NOT-BE-FETCHED")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/photo.png"
        with pytest.raises(ValueError, match="non-https"):
            download_slack_file("xoxb-test-token", url)
        assert hits == []
        with pytest.raises(ValueError, match="non-https"):
            download_slack_file("xoxb-test-token", "ftp://example.com/x.png")
    finally:
        server.shutdown()
        server.server_close()


def test_download_refuses_empty_token_or_url():
    with pytest.raises(ValueError):
        download_slack_file("", "https://files.slack.com/x.png")
    with pytest.raises(ValueError):
        download_slack_file("xoxb-test-token", "")


def test_download_errors_never_leak_token():
    token = "xoxb-secret-token-123"

    def failing_get(url, token_arg):
        raise ValueError("upstream boom")

    with pytest.raises(ValueError) as excinfo:
        download_slack_file(token, "https://files.slack.com/x.png",
                            get=failing_get)
    assert token not in str(excinfo.value)

    def no_url_get(url, token_arg):
        return {"ok": True, "file": {}}

    with pytest.raises(ValueError) as excinfo:
        download_slack_file(token, "F999", get=no_url_get)
    assert token not in str(excinfo.value)

    def dict_get(url, token_arg):
        return {"ok": True, "not": "bytes"}

    with pytest.raises(ValueError, match="did not return bytes"):
        download_slack_file(token, "https://files.slack.com/x.png",
                            get=dict_get)
