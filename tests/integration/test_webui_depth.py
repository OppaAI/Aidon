"""WebUI depth: .env settings, studio edits, plugin adoption.

Handler methods are called directly on WebUIServer, no real
server on a fixed port; one test also drives the HTTP routes
on an ephemeral localhost port, as in test_webui_msg.py.
"""

import json
import threading
from http.server import HTTPServer
from pathlib import Path

from src.capabilities.skill_library import Skill, SkillLibrary
from src.interface.webui import WebUIServer
from src.memory.memory import InMemoryStore, Point


def _server(tmp_path, memory=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return WebUIServer(home, memory=memory)


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


def test_settings_set_env_writes_secrets_write_only(tmp_path):
    server = _server(tmp_path)
    result = server.settings_set_env("timezone", "America/Vancouver")
    assert result == {"ok": True, "key": "AIDON_TIMEZONE"}
    env_text = (tmp_path / "home" / ".env").read_text(encoding="utf-8")
    assert "AIDON_TIMEZONE=America/Vancouver" in env_text
    assert "America/Vancouver" not in str(result)
    # Replace, not duplicate.
    server.settings_set_env("AIDON_TIMEZONE", "UTC")
    lines = (tmp_path / "home" / ".env").read_text(encoding="utf-8").splitlines()
    assert lines.count("AIDON_TIMEZONE=UTC") == 1
    assert not any(line.startswith("AIDON_TIMEZONE=America") for line in lines)
    # Owner-only file.
    assert (tmp_path / "home" / ".env").stat().st_mode & 0o777 == 0o600
    # Secrets are writable here: write-only,
    # never shown back, empty leaves the current value alone.
    for secret_key in ("AIDON_API_KEY", "AIDON_WHATSAPP_TOKEN",
                       "AIDON_SLACK_SIGNING_SECRET",
                       "AIDON_TRANSCRIBE_API_KEY"):
        written = server.settings_set_env(secret_key, "test-value")
        assert written["ok"] is True
        assert "test-value" not in str(written)
        assert f"{secret_key}=test-value" in (tmp_path / "home" / ".env").read_text(encoding="utf-8")
        kept = server.settings_set_env(secret_key, "")
        assert kept == {"ok": True, "key": secret_key, "unchanged": True}
        assert f"{secret_key}=test-value" in (tmp_path / "home" / ".env").read_text(encoding="utf-8")
    # GMAIL_CLIENT_SECRET is not a configured key (Gmail transport was
    # dropped), so it is still refused as an unknown key.
    assert server.settings_set_env("GMAIL_CLIENT_SECRET", "x")["ok"] is False
    # Unknown keys and newline values are refused, not written.
    assert server.settings_set_env("not_a_setting", "x")["ok"] is False
    assert server.settings_set_env("timezone", "a\nAIDON_API_KEY=evil")["ok"] is False
    assert "evil" not in (tmp_path / "home" / ".env").read_text(encoding="utf-8")


def test_settings_json_behaviour_unchanged(tmp_path):
    server = _server(tmp_path)
    assert server.settings_set("timezone", "UTC") is True
    assert server.settings_get()["timezone"] == "UTC"
    assert server.settings_set("AIDON_API_KEY", "x") is False
    assert not (tmp_path / "home" / ".env").exists()


def test_memory_edit_updates_live_point(tmp_path):
    store = InMemoryStore()
    point = Point(id="p1", text="old text", vector=[], payload={"text": "old text"})
    store.upsert("episodic", point)

    class FakeMemory:
        def __init__(self, store):
            self.store = store

    server = _server(tmp_path, memory=FakeMemory(store))
    result = server.handle_memory_edit("episodic", "p1", "new text")
    assert result["ok"] is True and result["via"] == "store"
    assert store.get("episodic", "p1").text == "new text"
    assert store.get("episodic", "p1").payload["text"] == "new text"


def test_memory_edit_without_store_is_recorded_not_dropped(tmp_path):
    server = _server(tmp_path, memory=None)
    result = server.handle_memory_edit("episodic", "missing-point", "edited text")
    assert result["ok"] is True and result["via"] == "recorded"
    record = json.loads((tmp_path / "home" / "memory_edits" / "missing-point.json").read_text(encoding="utf-8"))
    assert record["text"] == "edited text" and record["action"] == "edit"
    assert server.handle_memory_edit("episodic", "", "")["ok"] is False


def test_skill_edit_updates_library_and_records_missing(tmp_path):
    server = _server(tmp_path)
    library = SkillLibrary(tmp_path / "home")
    library.add(Skill(name="adder", description="old description"))
    result = server.handle_skill_edit("adder", "new description")
    assert result["ok"] is True and result["via"] == "library"
    assert SkillLibrary(tmp_path / "home").get("adder").description == "new description"
    result = server.handle_skill_edit("ghost", "some description")
    assert result["ok"] is True and result["via"] == "recorded"
    record = json.loads((tmp_path / "home" / "skill_edits" / "ghost.json").read_text(encoding="utf-8"))
    assert record["description"] == "some description"
    assert server.handle_skill_edit("", "")["ok"] is False


def test_plugin_adopt_needs_approval_and_copies_when_approved(tmp_path):
    server = _server(tmp_path)
    directory = _plugin_dir(tmp_path, "upper")
    refused = server.handle_plugin_adopt(str(directory), approve=lambda plugin, f: False)
    assert refused["ok"] is False
    assert not (tmp_path / "home" / "toolbox" / "upper").exists()
    approved = server.handle_plugin_adopt(str(directory), approve=lambda plugin, f: True)
    assert approved["ok"] is True
    assert (tmp_path / "home" / "toolbox" / "upper" / "tool.py").exists()
    assert server.handle_plugin_adopt("/nonexistent-dir")["ok"] is False


def test_http_routes_for_new_endpoints(tmp_path):
    import urllib.error
    import urllib.request

    server = _server(tmp_path)
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        def post(path, payload):
            data = json.dumps(payload).encode()
            req = urllib.request.Request(base + path, data=data, method="POST",
                                         headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req) as resp:
                    return resp.status, json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, payload = post("/api/settings/env", {"key": "timezone", "value": "UTC"})
        assert status == 200 and payload["ok"] is True and payload["key"] == "AIDON_TIMEZONE"
        assert "AIDON_TIMEZONE=UTC" in (tmp_path / "home" / ".env").read_text(encoding="utf-8")
        status, payload = post("/api/settings/env", {"key": "AIDON_API_KEY", "value": "x"})
        assert status == 200 and payload["ok"] is True
        assert "x" not in str(payload)
        assert "AIDON_API_KEY=x" in (tmp_path / "home" / ".env").read_text(encoding="utf-8")
        status, payload = post("/api/studio/memory", {"action": "edit", "collection": "episodic", "point_id": "p9", "text": "edited"})
        assert status == 200 and payload["via"] == "recorded"
        status, payload = post("/api/studio/capabilities", {"action": "edit", "name": "ghost2", "description": "d"})
        assert status == 200 and payload["via"] == "recorded"
        directory = _plugin_dir(tmp_path, "upper2")
        # Route path uses the server gate: no card is approved, so
        # adoption must not copy. Use a short gate timeout server
        # instead of waiting the default card clock.
        server.webui_gate.timeout_seconds = 1
        status, payload = post("/api/plugins/adopt", {"path": str(directory)})
        assert payload["ok"] is False
        assert not (tmp_path / "home" / "toolbox" / "upper2").exists()
    finally:
        httpd.shutdown()
