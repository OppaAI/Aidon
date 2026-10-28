"""Pre-approval from every surface: WebUI, WhatsApp, Slack, and the gate.

One store (<home>/preapprovals.json), one command parser, so a grant
made on a phone is the same grant the WebUI panel shows, and an
irreversible kind is refused the same way everywhere.
"""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import HTTPServer

from src.interface.approval import ApprovalGate, Decision
from src.interface.messaging import MessagingConfig
from src.interface.preapproval import (
    IRREVERSIBLE,
    KINDS,
    PreapprovalStore,
    handle_command,
    kind_from_token,
)
from src.interface.preapproval_gate import PreapprovalGate
from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer


# ── store and shared command parser ─────────────────────────────────

def test_grant_is_shared_across_store_instances(tmp_path):
    first = PreapprovalStore(tmp_path)
    reply = handle_command(first, "preapprove dinner live 2")
    assert "Pre-approved live for dinner" in reply
    # A second instance reads the same file: this is what makes a grant
    # made in WhatsApp visible in the WebUI.
    second = PreapprovalStore(tmp_path)
    assert second.is_preapproved("dinner", "live") is True
    assert second.is_preapproved("other", "live") is False


def test_kind_by_number_and_by_name(tmp_path):
    store = PreapprovalStore(tmp_path)
    number = 1  # "live" in the menu
    assert kind_from_token(str(number)) == KINDS[0]["name"]
    assert handle_command(store, f"preapprove dinner {number} 1").startswith(
        "Pre-approved")
    assert kind_from_token("spec") == "spec"


def test_irreversible_kind_is_refused_everywhere(tmp_path):
    store = PreapprovalStore(tmp_path)
    for kind in ("send_external", "publish", "spend", "delete", "install",
                 "plugin", "mandatory"):
        reply = handle_command(store, f"preapprove dinner {kind} 1")
        assert "can never be pre-approved" in reply
        assert store.grant("dinner", kind, time.time() + 3600) is False
        assert store.is_preapproved("dinner", kind) is False
    assert IRREVERSIBLE  # the set is not empty


def test_list_and_revoke_from_the_chat(tmp_path):
    store = PreapprovalStore(tmp_path)
    handle_command(store, "preapprove dinner spec 2")
    listing = handle_command(store, "preapprovals")
    assert "dinner · spec" in listing
    assert handle_command(store, "unpreapprove dinner spec").startswith("Revoked")
    assert handle_command(store, "preapprovals").startswith("No pre-approvals")
    # Revoking everything for a project also works.
    handle_command(store, "preapprove dinner spec 2")
    handle_command(store, "preapprove dinner live 2")
    assert "Revoked 2" in handle_command(store, "unpreapprove dinner all")


def test_expired_grant_does_not_apply(tmp_path):
    store = PreapprovalStore(tmp_path)
    store.grant("dinner", "live", until=time.time() - 1)
    assert store.is_preapproved("dinner", "live") is False
    assert store.active() == []


def test_plain_text_is_not_a_command(tmp_path):
    store = PreapprovalStore(tmp_path)
    assert handle_command(store, "plan a dinner for two") is None
    assert handle_command(store, "") is None


# ── gate: reversible only ───────────────────────────────────────────

class _Gate:
    def __init__(self):
        self.asked = []

    def ask(self, kind, title, body, **kwargs):
        self.asked.append(kind)
        return Decision("rejected")


def test_live_preapproval_needs_a_reversible_run(tmp_path):
    store = PreapprovalStore(tmp_path)
    store.grant("dinner", "live", time.time() + 3600)
    gate = _Gate()
    wrapped = PreapprovalGate(gate, store, "dinner")
    # Reversible run: the card is skipped.
    assert wrapped.ask("live", "Go live?", "body", reversible=True).verdict == "approved"
    assert gate.asked == []
    # Not reversible: the human is asked, the grant does not cover it.
    assert wrapped.ask("live", "Go live?", "body", reversible=False).verdict == "rejected"
    assert gate.asked == ["live"]


def test_irreversible_kinds_always_reach_the_human(tmp_path):
    store = PreapprovalStore(tmp_path)
    gate = _Gate()
    wrapped = PreapprovalGate(gate, store, "dinner")
    for kind in ("publish", "install", "plugin", "mandatory", "send_external"):
        wrapped.ask(kind, "t", "b", reversible=True)
    assert gate.asked == ["publish", "install", "plugin", "mandatory",
                          "send_external"]


def test_reversible_grant_skips_the_card(tmp_path):
    store = PreapprovalStore(tmp_path)
    store.grant("dinner", "spec", time.time() + 3600)
    gate = _Gate()
    wrapped = PreapprovalGate(gate, store, "dinner")
    assert wrapped.ask("spec", "Approve spec?", "body").verdict == "approved"
    assert gate.asked == []


def test_wrapper_exposes_the_inner_gate(tmp_path):
    gate = ApprovalGate(lambda kind, title, body: Decision("approved"),
                        timeout_seconds=7)
    wrapped = PreapprovalGate(gate, PreapprovalStore(tmp_path), "dinner")
    assert wrapped.timeout_seconds == 7
    assert wrapped.policy is None


# ── WebUI, WhatsApp, Slack ──────────────────────────────────────────

def _server(tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    dispatcher = TriggerDispatcher()
    dispatcher.register("webui", lambda e: e.send_reply(f"WebUI got: {e.goal}"))
    dispatcher.register("whatsapp", lambda e: e.send_reply(f"WhatsApp got: {e.goal}"))
    dispatcher.register("slack", lambda e: e.send_reply(f"Slack got: {e.goal}"))
    whatsapp_replies, slack_replies = [], []
    config = MessagingConfig(
        whatsapp_token="fake-wa", whatsapp_phone_id="12345",
        whatsapp_verify_token="verify123", whatsapp_app_secret="",
        whatsapp_owner="+16045551234",
        slack_bot_token="xoxb-fake", slack_signing_secret="")
    server = WebUIServer(
        home, dispatcher=dispatcher, messaging_config=config,
        whatsapp_sender=lambda to, text: whatsapp_replies.append((to, text)),
        slack_sender=lambda channel, text, thread_ts=None:
            slack_replies.append((channel, text, thread_ts)))
    return server, whatsapp_replies, slack_replies


def test_webui_chat_command_grants_and_lists(tmp_path):
    server, _, _ = _server(tmp_path)
    reply = server.handle_chat({"message": "preapprove dinner live 3"})["reply"]
    assert "Pre-approved live for dinner" in reply
    listing = server.handle_chat({"message": "preapprovals"})["reply"]
    assert "dinner · live" in listing
    # A normal message still becomes a goal.
    assert server.handle_chat({"message": "Plan a dinner"})["reply"] == \
        "WebUI got: Plan a dinner"


def test_whatsapp_command_grants_and_confirms(tmp_path):
    server, replies, _ = _server(tmp_path)
    body = {"entry": [{"changes": [{"value": {"messages": [
        {"from": "+16045551234", "text": {"body": "preapprove dinner 2 1"}}]}}]}]}
    status, result = server.handle_whatsapp_post(body)
    assert status == 200
    assert result["replies"] and "Pre-approved spec for dinner" in result["replies"][0]
    assert replies[0][0] == "+16045551234"
    assert PreapprovalStore(server.home).is_preapproved("dinner", "spec")


def test_slack_command_grants_and_confirms(tmp_path):
    server, _, replies = _server(tmp_path)
    body = {"type": "event", "event": {
        "type": "message", "text": "preapprove dinner live 4",
        "channel": "C123", "ts": "1700000000.000100"}}
    status, result = server.handle_slack_post(json.dumps(body).encode(), {}, body)
    assert status == 200
    assert "Pre-approved live for dinner" in result["replies"][0]
    assert replies[0][0] == "C123"
    assert PreapprovalStore(server.home).is_preapproved("dinner", "live")


def test_webui_routes_grant_list_revoke(tmp_path):
    server, _, _ = _server(tmp_path)
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def request(method, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        status, payload = request("GET", "/api/preapprovals")
        assert status == 200 and payload["grants"] == []
        assert [kind["name"] for kind in payload["kinds"]] == \
            [kind["name"] for kind in KINDS]

        status, payload = request("POST", "/api/preapprovals",
                                  {"spec_id": "dinner", "kind": "1", "hours": 2})
        assert status == 200 and payload["kind"] == "live"

        status, payload = request("GET", "/api/preapprovals")
        assert [g["action_kind"] for g in payload["grants"]] == ["live"]

        # Irreversible kinds are refused with a reason, not a 200.
        status, payload = request("POST", "/api/preapprovals",
                                  {"spec_id": "dinner", "kind": "publish"})
        assert status == 400 and "never be pre-approved" in payload["error"]

        status, payload = request("DELETE", "/api/preapprovals/dinner/live")
        assert status == 200 and payload["removed"] == 1
        status, payload = request("GET", "/api/preapprovals")
        assert payload["grants"] == []
    finally:
        httpd.shutdown()


def test_settings_page_has_the_preapproval_panel(tmp_path):
    server, _, _ = _server(tmp_path)
    httpd = HTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/settings") as response:
            page = response.read().decode()
        assert 'id="preapprovals"' in page
        assert 'id="pre-grant"' in page
        with urllib.request.urlopen(base + "/static/settings.js") as response:
            script = response.read().decode()
        assert "/api/preapprovals" in script
    finally:
        httpd.shutdown()
