"""WebUI approval cards must be visible and decidable from the chat page.

Regression: the chat page had <div id="approval"> but no JS touched it,
no route listed pending cards, and POST /api/approve needed a card id
the user was never shown. Also: a timed-out card kept verdict None, so
it would never have left a pending list even once one existed.
"""
import threading
import time
import urllib.request
import json
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from src.interface.webui import CHAT_HTML, WebUIGate, WebUIServer


def _wait_for_card(gate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gate.cards:
            return next(iter(gate.cards))
        time.sleep(0.01)
    raise AssertionError("no card appeared")


def test_pending_cards_lists_open_card():
    gate = WebUIGate(timeout_seconds=60)
    thread = threading.Thread(target=gate._decide,
                              args=("live", "Run the plan", "It touches real files"))
    thread.start()
    try:
        card_id = _wait_for_card(gate)
        pending = gate.pending_cards()
        assert pending == [{"id": card_id, "kind": "live",
                            "title": "Run the plan", "body": "It touches real files"}]
    finally:
        gate.approve(_wait_for_card(gate), "rejected")
        thread.join(timeout=5)


def test_approved_card_leaves_pending_list():
    gate = WebUIGate(timeout_seconds=60)
    thread = threading.Thread(target=gate._decide, args=("live", "T", "B"))
    thread.start()
    try:
        card_id = _wait_for_card(gate)
        assert gate.approve(card_id, "approved") is True
        thread.join(timeout=5)
        assert gate.pending_cards() == []
    finally:
        if thread.is_alive():
            gate.approve(_wait_for_card(gate), "rejected")
            thread.join(timeout=5)


def test_rejected_card_leaves_pending_list():
    gate = WebUIGate(timeout_seconds=60)
    thread = threading.Thread(target=gate._decide, args=("plan", "T", "B"))
    thread.start()
    try:
        card_id = _wait_for_card(gate)
        assert gate.approve(card_id, "rejected") is True
        thread.join(timeout=5)
        assert gate.pending_cards() == []
    finally:
        if thread.is_alive():
            gate.approve(_wait_for_card(gate), "rejected")
            thread.join(timeout=5)


def test_edited_card_preserves_edit_and_is_not_approval():
    gate = WebUIGate(timeout_seconds=60)
    result = []
    thread = threading.Thread(
        target=lambda: result.append(
            gate._decide("live", "T", "original")))
    thread.start()
    try:
        card_id = _wait_for_card(gate)
        assert gate.approve(card_id, "edited", "revised") is True
        thread.join(timeout=5)
        assert result[0].verdict == "edited"
        assert result[0].edited_text == "revised"
    finally:
        if thread.is_alive():
            gate.approve(_wait_for_card(gate), "rejected")
            thread.join(timeout=5)


def test_timed_out_card_leaves_pending_list():
    gate = WebUIGate(timeout_seconds=0.05)
    thread = threading.Thread(target=gate._decide, args=("live", "T", "B"))
    thread.start()
    thread.join(timeout=5)
    assert gate.pending_cards() == []
    assert gate.cards["card-1"]["verdict"] == "rejected"


def test_cancel_all_clears_pending_list():
    gate = WebUIGate(timeout_seconds=60)
    thread = threading.Thread(target=gate._decide, args=("live", "T", "B"))
    thread.start()
    try:
        _wait_for_card(gate)
        gate.cancel_all()
        thread.join(timeout=5)
        assert gate.pending_cards() == []
    finally:
        if thread.is_alive():
            gate.approve(_wait_for_card(gate), "rejected")
            thread.join(timeout=5)


class _Config:
    def __init__(self, timeout):
        self.approval_timeout_seconds = timeout


def test_server_gate_timeout_defaults_to_900(tmp_path):
    server = WebUIServer(tmp_path / "home")
    assert server.webui_gate.timeout_seconds == 900


def test_server_gate_timeout_comes_from_config(tmp_path):
    server = WebUIServer(tmp_path / "home", config=_Config(42))
    assert server.webui_gate.timeout_seconds == 42


def test_server_gate_timeout_none_config_falls_back_to_900(tmp_path):
    server = WebUIServer(tmp_path / "home", config=_Config(None))
    assert server.webui_gate.timeout_seconds == 900


@pytest.fixture
def live_server(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    server = WebUIServer(home, memory=None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        yield base, server
    finally:
        httpd.shutdown()
        httpd.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path) as resp:
        return resp.status, json.loads(resp.read())


def _post(base, path, payload):
    req = urllib.request.Request(base + path, method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read())


def test_approvals_route_round_trip(live_server):
    base, server = live_server
    status, data = _get(base, "/api/approvals")
    assert status == 200
    assert data == {"cards": []}
    gate = server.webui_gate
    thread = threading.Thread(target=gate._decide, args=("live", "Ship it", "Publishes"))
    thread.start()
    try:
        card_id = _wait_for_card(gate)
        status, data = _get(base, "/api/approvals")
        assert status == 200
        assert data == {"cards": [{"id": card_id, "kind": "live",
                                    "title": "Ship it", "body": "Publishes"}]}
        status, result = _post(base, "/api/approve",
                               {"id": card_id, "verdict": "approved"})
        assert status == 200
        assert result == {"ok": True}
        thread.join(timeout=5)
        status, data = _get(base, "/api/approvals")
        assert data == {"cards": []}
    finally:
        if thread.is_alive():
            gate.approve(_wait_for_card(gate), "rejected")
            thread.join(timeout=5)


def test_chat_page_polls_and_decides_approvals():
    # The approval div is now driven: poll, render, decide, refresh.
    assert 'fetch(\'/api/approvals\')' in CHAT_HTML
    assert 'fetch(\'/api/approve\'' in CHAT_HTML
    assert 'refreshApprovals' in CHAT_HTML
    assert 'setInterval(refreshApprovals, 2000)' in CHAT_HTML
    assert 'getElementById(\'approval\')' in CHAT_HTML
    # Untouched: avatar CSS, markdown rendering, pill refresh.
    assert 'robot-bob' in CHAT_HTML
    assert 'render_markdown' in CHAT_HTML
    assert 'setInterval(refreshPills, 2000)' in CHAT_HTML


def test_whatsapp_card_delivery_on_creation():
    """A card for a WhatsApp-bound run is delivered at once.

    The card carries its run context; when the context names
    a WhatsApp destination, the full card — title, body, id,
    and the approve/reject reply words — goes to the owner
    immediately, instead of the owner never learning the id.
    Without a WhatsApp destination, nothing is sent.
    """
    sent = []
    gate = WebUIGate(timeout_seconds=0.5,
                     whatsapp_sender=lambda to, text: sent.append((to, text)))
    decision = gate.gate.ask(
        "live", "Run the plan",
        "The plan writes report.md.",
        context={"run_id": "r1", "conversation_id": "c1",
                 "source": "webui", "whatsapp_to": "+10000000000"})
    assert decision.verdict == "rejected"  # timed out unanswered: fail-closed
    assert len(sent) == 1
    to, text = sent[0]
    assert to == "+10000000000"
    assert "card-1" in text
    assert "Run the plan" in text
    assert "report.md" in text
    assert "approve card-1" in text
    assert "reject card-1" in text
    assert gate.cards["card-1"]["context"]["run_id"] == "r1"

    sent.clear()
    plain = WebUIGate(timeout_seconds=0.5,
                      whatsapp_sender=lambda to, text: sent.append((to, text)))
    plain.gate.ask("live", "Run the plan", "The plan writes report.md.")
    assert sent == []


def test_nudge_fires_before_timeout_with_title():
    """The Slack nudge beats the card timeout and names the card.

    A nudge configured for longer than the card timeout used
    to arrive only after the card had already failed closed,
    carrying a bare id. It now fires at half the timeout and
    includes the title.
    """
    from src.interface.scheduler_loop import NudgeTracker
    nudges = []
    gate = WebUIGate(
        timeout_seconds=4,
        nudge_tracker=NudgeTracker(nudge_minutes=15, channel="#owner"),
        slack_sender=lambda channel, text: nudges.append((channel, text)))
    gate.gate.ask("live", "Run the plan", "The plan writes report.md.")
    assert len(nudges) == 1
    channel, text = nudges[0]
    assert channel == "#owner"
    assert "card-1" in text
    assert "Run the plan" in text


def test_nudge_skipped_when_card_decided():
    """An approved card gets no nudge."""
    import threading
    from src.interface.scheduler_loop import NudgeTracker
    nudges = []
    gate = WebUIGate(
        timeout_seconds=4,
        nudge_tracker=NudgeTracker(nudge_minutes=15, channel="#owner"),
        slack_sender=lambda channel, text: nudges.append((channel, text)))
    threading.Timer(0.5, lambda: gate.approve("card-1", "approved")).start()
    decision = gate.gate.ask("live", "Run the plan", "body")
    assert decision.verdict == "approved"
    time.sleep(2.5)
    assert nudges == []


def test_gate_context_forwarded_only_when_decide_accepts_it():
    """ApprovalGate forwards context to context-aware decides only."""
    from src.interface.approval import ApprovalGate, Decision
    seen = {}
    gate = ApprovalGate(
        lambda kind, title, body, context: seen.update(context) or Decision("approved"))
    gate.ask("live", "t", "b", context={"run_id": "r1"})
    assert seen == {"run_id": "r1"}
    plain = ApprovalGate(lambda kind, title, body: Decision("approved"))
    assert plain.ask("live", "t", "b",
                     context={"run_id": "r1"}).verdict == "approved"


def test_config_approval_timeout_defaults_to_900(tmp_path):
    """The tunable approval timeout defaults to 900 seconds."""
    from src.system.config import Config
    assert Config(tmp_path / "home").approval_timeout_seconds == 900


def test_transcribe_stream_routes_without_key(live_server):
    """Stream endpoints are wired and fail safe without a key.

    Start with no transcribe key returns 501 (no Meta call is
    made); audio/end for an unknown session return 404.
    """
    import urllib.error
    base, server = live_server
    assert server.transcribe_key is None
    for path, payload, code in [
            ("/api/transcribe_stream/start", {}, 501),
            ("/api/transcribe_stream/audio",
             {"session_id": "nope", "audio_b64": ""}, 404),
            ("/api/transcribe_stream/end", {"session_id": "nope"}, 404)]:
        try:
            _post(base, path, payload)
        except urllib.error.HTTPError as exc:
            assert exc.code == code, (path, exc.code)
        else:
            raise AssertionError(f"expected HTTP {code} for {path}")
