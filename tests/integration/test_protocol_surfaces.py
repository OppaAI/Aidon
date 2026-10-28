"""Protocol surface coverage: MCP HTTP transport, live model routing, remote
hub server, and the new WebUI routes. No real endpoint, no real
key, no fixed port: every server here is a local double on an
ephemeral localhost port, and every model is a fake client.
"""

import base64
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.capabilities.remote_hub_server import HubClient, HubServer
from src.interface.mcp_http import McpHttpClient, McpHttpError
from src.interface.triggers import TriggerDispatcher
from src.interface.webui import VRM_HTML, WebUIServer
from src.system.model_client import FakeClient, Message
from src.system.model_router import RouterClient, client_for_routing


# ---------- helpers ----------

def _request(base, method, path, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
    try:
        with urllib.request.urlopen(req) as resp:
            text = resp.read().decode()
            try:
                return resp.status, json.loads(text)
            except json.JSONDecodeError:
                return resp.status, text
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        try:
            return exc.code, json.loads(text)
        except json.JSONDecodeError:
            return exc.code, text


class _McpDouble:
    """A JSON-RPC MCP server double, with optional token auth."""

    def __init__(self, token=None, sse=False):
        self.token = token
        self.sse = sse
        self.seen = []

    def make_handler(self):
        double = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, content_type="application/json"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if double.token:
                    auth = self.headers.get("Authorization", "")
                    if auth != f"Bearer {double.token}":
                        self._send(401, json.dumps({"error": "unauthorized"}))
                        return
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                double.seen.append(body)
                method = body.get("method")
                req_id = body.get("id")
                if method == "initialize":
                    result = {"protocolVersion": "2024-11-05",
                              "serverInfo": {"name": "gmail-mcp", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": [{"name": "gmail_send",
                                         "description": "send an email"}]}
                elif method == "tools/call":
                    args = body["params"]["arguments"]
                    result = {"content": [{"type": "text",
                                           "text": f"sent to {args.get('to', '')}"}]}
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
                if double.sse:
                    self._send(200, f"event: message\ndata: {payload}\n\n",
                               "text/event-stream")
                else:
                    self._send(200, payload)

            def log_message(self, *args):
                pass

        return Handler

    def start(self):
        httpd = HTTPServer(("127.0.0.1", 0), self.make_handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


# ---------- MCP HTTP ----------

def test_mcp_http_initialize_list_call():
    double = _McpDouble(token="tok-1")
    httpd, base = double.start()
    try:
        client = McpHttpClient(base, token="tok-1")
        info = client.initialize()
        assert info["serverInfo"]["name"] == "gmail-mcp"
        tools = client.list_tools()
        assert [t["name"] for t in tools] == ["gmail_send"]
        result = client.call_tool("gmail_send", {"to": "a@example.com"})
        assert "a@example.com" in result["content"][0]["text"]
        client.close()
    finally:
        httpd.shutdown()


def test_mcp_http_sse_response_parses():
    double = _McpDouble(sse=True)
    httpd, base = double.start()
    try:
        client = McpHttpClient(base)
        client.initialize()
        tools = client.list_tools()
        assert tools[0]["name"] == "gmail_send"
    finally:
        httpd.shutdown()


def test_mcp_http_401_without_token():
    double = _McpDouble(token="tok-1")
    httpd, base = double.start()
    try:
        with pytest.raises(McpHttpError, match="HTTP 401"):
            McpHttpClient(base).initialize()
    finally:
        httpd.shutdown()


def test_mcp_http_secret_never_a_tool_argument():
    client = McpHttpClient("http://127.0.0.1:9", token="secret-token")
    with pytest.raises(ValueError, match="tool argument"):
        client.call_tool("gmail_send", {"body": "my secret-token here"})


# ---------- model router ----------

def test_router_phase_dispatch_and_fallback():
    main = FakeClient("main-reply")
    verifier = FakeClient("verify-reply")
    router = RouterClient({"verify": verifier}, default=main)
    assert router.route("verify") is verifier
    assert router.route("plan") is main
    assert router.route("") is main
    assert router.chat("verify", [Message("user", "check")]) == "verify-reply"
    assert router.chat("plan", [Message("user", "do")]) == "main-reply"
    assert router.chat([Message("user", "hi")]) == "main-reply"


def test_router_from_config():
    class Cfg:
        model = "main-model"
        fast_model = "fast-model"
        verifier_model = "verifier-model"
        router_fast_phases = "interpret,respond"

    clients = {"main-model": FakeClient("main"),
               "fast-model": FakeClient("fast"),
               "verifier-model": FakeClient("verifier")}
    router = client_for_routing(Cfg(), clients)
    assert router.chat("interpret", [Message("user", "x")]) == "fast"
    assert router.chat("verify", [Message("user", "x")]) == "verifier"
    assert router.chat("live", [Message("user", "x")]) == "main"


def test_router_from_config_missing_verifier_falls_back():
    class Cfg:
        model = "main-model"
        fast_model = ""
        router_fast_phases = ""

    router = client_for_routing(Cfg(), {"main-model": FakeClient("main")})
    assert router.chat("verify", [Message("user", "x")]) == "main"


# ---------- remote hub server ----------

def test_hub_server_auth_and_round_trip(tmp_path):
    server = HubServer(tmp_path, token="hub-token")
    httpd, _, base = server.start()
    try:
        client = HubClient(base, token="hub-token")
        assert client.health() == {"ok": True}
        dag = {"id": "dag-1", "goal": "demo goal", "nodes": []}
        assert client.push_dag(dag)["ok"] is True
        assert client.pull_dag("dag-1") == dag
        assert client.list_dags() == ["dag-1"]
        mission = {"id": "m-1", "goal": "mission goal"}
        assert client.push_mission(mission)["ok"] is True
        assert client.pull_mission("m-1") == mission
        assert client.list_missions() == ["m-1"]
        # Files really landed under <home>/hub/.
        assert (tmp_path / "hub" / "dags" / "dag-1.json").is_file()
        assert (tmp_path / "hub" / "missions" / "m-1.json").is_file()
        # No token, no access.
        with pytest.raises(ValueError, match="HTTP 401"):
            HubClient(base).list_dags()
        # A record without an id is rejected, not stored.
        with pytest.raises(ValueError, match="HTTP 400"):
            client.push_dag({"goal": "no id"})
    finally:
        httpd.shutdown()


# ---------- WebUI new routes ----------

def _start_webui(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    server = WebUIServer(home, dispatcher=TriggerDispatcher())
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return server, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def test_webui_tools_conversations_brief_ideas(tmp_path):
    server, httpd, base = _start_webui(tmp_path)
    try:
        status, data = _request(base, "GET", "/api/tools")
        assert status == 200
        assert "write" in data["sandbox_tools"]
        status, conv = _request(base, "POST", "/api/conversations",
                                {"project_id": "proj-a"})
        assert status == 200 and conv["project_id"] == "proj-a"
        conv_id = conv["id"]
        status, data = _request(base, "GET", "/api/conversations")
        assert any(c["id"] == conv_id for c in data["conversations"])
        status, data = _request(base, "GET", f"/api/conversations/{conv_id}")
        assert status == 200 and data["id"] == conv_id
        status, data = _request(base, "PUT", "/api/projects/proj-a/brief",
                                {"brief": {"goal": "dinner"}})
        assert status == 200 and data["brief"]["goal"] == "dinner"
        status, data = _request(base, "GET", "/api/projects/proj-a/brief")
        assert status == 200 and data["brief"]["goal"] == "dinner"
        status, idea = _request(base, "POST", "/api/ideas", {"text": "an idea"})
        assert status == 200 and idea["text"] == "an idea"
        idea_id = idea["id"]
        status, data = _request(base, "GET", "/api/ideas")
        assert any(i["id"] == idea_id for i in data["ideas"])
        status, data = _request(base, "DELETE", f"/api/ideas/{idea_id}")
        assert status == 200 and data["ok"] is True
        status, data = _request(base, "DELETE", f"/api/conversations/{conv_id}")
        assert status == 200 and data["ok"] is True
        status, _ = _request(base, "GET", f"/api/conversations/{conv_id}")
        assert status == 404
    finally:
        httpd.shutdown()


def test_webui_hub_runs_and_report(tmp_path):
    _, httpd, base = _start_webui(tmp_path)
    try:
        status, data = _request(base, "POST", "/api/hub/runs",
                                {"id": "run-1", "goal": "demo"})
        assert status == 200 and data["id"] == "run-1"
        for suffix, payload in (("events", {"kind": "started"}),
                                ("stage", {"stage": "plan", "status": "done"}),
                                ("trace", {"kind": "plan", "status": "done"})):
            status, data = _request(base, "POST", f"/api/hub/runs/run-1/{suffix}", payload)
            assert status == 200, (suffix, data)
        stored = json.loads((tmp_path / "home" / "hub_runs" / "run-1.json").read_text())
        assert stored["events"] == [{"kind": "started"}]
        assert stored["stages"] == [{"stage": "plan", "status": "done"}]
        assert stored["traces"] == [{"kind": "plan", "status": "done"}]
        status, data = _request(base, "POST", "/api/reports/generate",
                                {"day": "2026-10-26", "summary": "did the thing"})
        assert status == 200
        report_path = tmp_path / "home" / "reports" / "2026-10-26.md"
        assert report_path.is_file()
        assert "did the thing" in report_path.read_text()
    finally:
        httpd.shutdown()


def test_webui_tts_501_without_key_and_goals_with_images(tmp_path):
    _, httpd, base = _start_webui(tmp_path)
    try:
        status, data = _request(base, "POST", "/api/tts", {"text": "hello"})
        assert status == 501
        image_b64 = base64.b64encode(b"fake-png-bytes").decode()
        status, data = _request(base, "POST", "/api/goals/with-images",
                                {"goal": "make a poster",
                                 "images": [{"name": "poster.png", "data_b64": image_b64}]})
        assert status == 200
        assert data["goal"] == "make a poster"
        assert data["images"] == ["poster.png"]
        assert data["run_id"]
        assert list((tmp_path / "home" / "library" / "uploads").glob("*-poster.png"))
    finally:
        httpd.shutdown()


def test_vrm_page_includes_lip_sync_js():
    assert "AnalyserNode" in VRM_HTML
    assert "Fcl_MTH" in VRM_HTML
    assert "Fcl_EYE_Close" in VRM_HTML
