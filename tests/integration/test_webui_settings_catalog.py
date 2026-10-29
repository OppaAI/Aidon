"""WebUI settings catalog and project deletion.

Handler methods are called directly on WebUIServer, no real
server on a fixed port; one test also drives the HTTP routes
on an ephemeral localhost port, as in test_webui_depth.py.
"""

import json
import threading
from http.server import HTTPServer
from pathlib import Path

from src.interface.webui import WebUIServer
from src.memory.conversations_db import ConversationStoreDB
from src.memory.embedder import HashEmbedder
from src.memory.memory import InMemoryStore, Memory


def _server(tmp_path, memory=None):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    return WebUIServer(home, memory=memory)


def _catalog_keys(server):
    return {entry["key"] for group in server.settings_catalog()["groups"]
            for entry in group["settings"]}


def test_catalog_groups_and_dead_keys_absent(tmp_path):
    server = _server(tmp_path)
    catalog = server.settings_catalog()
    assert [group["group"] for group in catalog["groups"]] == [
        "Model", "Sandbox", "Memory", "Research", "Messaging", "WebUI", "Loop"]
    keys = _catalog_keys(server)
    assert "AIDON_MODEL" in keys and "AIDON_BACKEND" in keys
    assert "AIDON_QDRANT_URL" in keys and "AIDON_WEBUI_TOKEN" in keys
    for dead in ("AIDON_META_API_KEY", "AIDON_META_MODEL", "AIDON_META_ENDPOINT",
                 "AIDON_DEPTH"):
        assert dead not in keys
    assert "AIDON_VERIFIER_MODEL" in keys
    assert "AIDON_THINK_PHASES" in keys
    # Enum options are carried for enum entries only.
    backend = [entry for group in catalog["groups"] for entry in group["settings"]
               if entry["key"] == "AIDON_BACKEND"][0]
    assert backend["type"] == "enum" and backend["options"] == ["meta", "native", "http", "fake"]
    assert "options" not in [entry for group in catalog["groups"] for entry in group["settings"]
                             if entry["key"] == "AIDON_MODEL"][0]


def test_catalog_masks_secrets_and_reports_is_set(tmp_path):
    server = _server(tmp_path)
    (tmp_path / "home" / ".env").write_text(
        "AIDON_API_KEY=real-secret-value\nAIDON_TIMEZONE=America/Vancouver\n",
        encoding="utf-8")
    catalog = server.settings_catalog()
    entries = {entry["key"]: entry for group in catalog["groups"]
               for entry in group["settings"]}
    secret = entries["AIDON_API_KEY"]
    assert secret["secret"] is True
    assert secret["value"] == "****" and secret["is_set"] is True
    assert "real-secret-value" not in json.dumps(catalog)
    assert entries["AIDON_TIMEZONE"]["value"] == "America/Vancouver"
    assert entries["AIDON_TIMEZONE"]["from_file"] is True
    assert entries["AIDON_MODEL"]["value"] == "muse-spark"  # default
    assert entries["AIDON_MODEL"]["from_file"] is False
    # An unset secret reports is_set False, not a value.
    assert entries["AIDON_WHATSAPP_TOKEN"]["is_set"] is False
    assert entries["AIDON_WHATSAPP_TOKEN"]["value"] == ""


def test_settings_set_env_validates_types(tmp_path):
    server = _server(tmp_path)
    assert server.settings_set_env("max_steps", "42")["ok"] is True
    assert "AIDON_MAX_STEPS=42" in (tmp_path / "home" / ".env").read_text()
    bad = server.settings_set_env("max_steps", "not-a-number")
    assert bad["ok"] is False
    assert server.settings_set_env("autonomy_enabled", "yes")["ok"] is True
    assert "AIDON_AUTONOMY_ENABLED=1" in (tmp_path / "home" / ".env").read_text()
    assert server.settings_set_env("autonomy_enabled", "maybe")["ok"] is False
    assert server.settings_set_env("backend", "native")["ok"] is True
    assert server.settings_set_env("backend", "bogus")["ok"] is False
    assert server.settings_set_env("search_backend", "searxng")["ok"] is True
    assert server.settings_set_env("execution_backend", "docker")["ok"] is True
    # Float-valued keys must parse as numbers.
    assert server.settings_set_env("forget_half_life_days", "21.5")["ok"] is True
    assert server.settings_set_env("forget_half_life_days", "soon")["ok"] is False
    # Every non-secret catalog key is writable.
    for key in ("AIDON_MODEL_THINK", "AIDON_ROUTER_FAST_PHASES",
                "AIDON_THREADS_ACCOUNT_ID", "AIDON_IG_USER_ID",
                "AIDON_APPROVAL_NUDGE_CHANNEL", "AIDON_KERNEL"):
        value = "1" if key in ("AIDON_KERNEL", "AIDON_MODEL_THINK") else "test-value"
        assert server.settings_set_env(key, value)["ok"] is True, key


def test_settings_set_env_writes_secrets_unknown_and_newlines(tmp_path):
    server = _server(tmp_path)
    (tmp_path / "home" / ".env").write_text("AIDON_API_KEY=keep-me\n", encoding="utf-8")
    for secret in ("AIDON_API_KEY", "AIDON_WHATSAPP_TOKEN",
                   "AIDON_THREADS_ACCESS_TOKEN", "AIDON_WEBUI_TOKEN"):
        written = server.settings_set_env(secret, "new-value")
        assert written["ok"] is True
        assert "new-value" not in str(written)
    # Empty secret value is a no-op: it must not clear the key.
    result = server.settings_set_env("AIDON_API_KEY", "")
    assert result["ok"] is True and result.get("unchanged") is True
    assert "AIDON_API_KEY=new-value" in (tmp_path / "home" / ".env").read_text()
    assert server.settings_set_env("AIDON_NO_SUCH_KEY", "x")["ok"] is False
    assert server.settings_set_env("timezone", "a\nb")["ok"] is False
    assert "a\nb" not in (tmp_path / "home" / ".env").read_text()


def test_project_delete_removes_conversations_and_memory(tmp_path):
    memory = Memory(tmp_path / "mem", embedder=HashEmbedder(), store=InMemoryStore())
    memory.record("episodic", "hello project", payload={"project_id": "proj-a"})
    memory.record("episodic", "other project", payload={"project_id": "proj-b"})
    server = _server(tmp_path, memory=memory)
    db = ConversationStoreDB(server.home)
    conv = db.create_conversation("proj-a")
    db.record_exchange(conv["id"], "user", "hi")
    other = db.create_conversation("proj-b")
    db.record_exchange(other["id"], "user", "hey")
    result = server.handle_project_delete("proj-a")
    assert result["ok"] is True
    assert result["removed_conversations"] == 1
    assert db.list_conversations("proj-a") == []
    assert len(db.list_conversations("proj-b")) == 1
    assert memory.list_points("episodic") == [
        point for point in memory.list_points("episodic")
        if point.payload.get("project_id") == "proj-b"]
    # Unknown project: ok False, nothing touched.
    assert server.handle_project_delete("no-such-project")["ok"] is False
    assert len(db.list_conversations("proj-b")) == 1


def test_project_delete_without_memory(tmp_path):
    server = _server(tmp_path, memory=None)
    db = ConversationStoreDB(server.home)
    conv = db.create_conversation("proj-a")
    db.record_exchange(conv["id"], "user", "hi")
    assert server.handle_project_delete("proj-a")["ok"] is True
    assert db.list_conversations("proj-a") == []
    assert server.handle_project_delete("proj-a")["ok"] is False


def _start_webui(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    server = WebUIServer(home)
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return server, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def _request(base, method, path, body=None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_http_catalog_and_project_delete_routes(tmp_path):
    server, httpd, base = _start_webui(tmp_path)
    try:
        status, data = _request(base, "GET", "/api/settings/catalog")
        assert status == 200
        assert [group["group"] for group in data["groups"]][0] == "Model"
        assert all(entry["value"] in ("", "****") for group in data["groups"]
                   for entry in group["settings"] if entry["secret"])
        # POST /api/settings still handles settings.json keys…
        status, data = _request(base, "POST", "/api/settings",
                                {"key": "timezone", "value": "UTC"})
        assert status == 200 and data["ok"] is True
        # …and catalog env keys.
        status, data = _request(base, "POST", "/api/settings",
                                {"key": "AIDON_MAX_STEPS", "value": "33"})
        assert status == 200 and data["ok"] is True
        assert "AIDON_MAX_STEPS=33" in (tmp_path / "home" / ".env").read_text()
        status, data = _request(base, "POST", "/api/settings",
                             {"key": "AIDON_API_KEY", "value": "nope"})
        assert status == 200 and data["ok"] is True
        assert "nope" not in str(data)
        assert "AIDON_API_KEY=nope" in (tmp_path / "home" / ".env").read_text()
        # Project delete route.
        conv = server.handle_conversation_create({"project_id": "proj-x"})
        status, data = _request(base, "DELETE", "/api/projects/proj-x")
        assert status == 200 and data["ok"] is True
        status, _ = _request(base, "DELETE", "/api/projects/proj-x")
        assert status == 404
    finally:
        httpd.shutdown()


def test_catalog_carries_vm_and_revision_settings(tmp_path):
    server = _server(tmp_path)
    entries = {entry["key"]: entry for group in server.settings_catalog()["groups"]
               for entry in group["settings"]}
    backend = entries["AIDON_EXECUTION_BACKEND"]
    assert "vm" in backend["options"]
    assert entries["AIDON_VM_HOST"]["default"] == ""
    assert entries["AIDON_VM_USER"]["default"] == ""
    assert entries["AIDON_VM_USE_SUDO"]["default"] == "0"
    password = entries["AIDON_VM_SUDO_PASSWORD"]
    assert password["secret"] is True
    assert "stdin" in password["desc"]
    revision = entries["AIDON_EMBEDDING_REVISION"]
    assert revision["default"] == "f9972ac22e696a105c482202646979e33e4b5a1e"
    assert "Default:" in revision["desc"]
    assert "Default:" in entries["AIDON_VM_HOST"]["desc"]
