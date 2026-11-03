"""Optional real-browser regression for approval and desktop-input ordering."""

import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

playwright = pytest.importorskip("playwright.sync_api")

from src.interface.triggers import TriggerDispatcher
from src.interface.webui import WebUIServer
from src.system.goal_tools import _goal_extra_tools


def test_overlay_is_gone_before_action_and_repeated_cards_remain_usable(tmp_path, monkeypatch):
    executed = []

    class Driver:
        def __call__(self, action):
            executed.append(action)

    monkeypatch.setattr("src.capabilities.x11_driver.X11Driver", Driver)
    dispatcher = TriggerDispatcher()
    server = WebUIServer(tmp_path, dispatcher=dispatcher)

    def handle(event):
        run_id = event.payload["run_id"]
        tools = _goal_extra_tools(
            SimpleNamespace(home=tmp_path, computer_driver="x11", kernel_mode=False),
            tmp_path / "work", gate=server.webui_gate.gate,
            gate_context={"run_id": run_id}, optional_tools=False,
            desktop_action_callback=event.payload["_desktop_action_callback"])
        result = json.loads(tools["computer_use"]([
            {"kind": "key", "key": "Tab"},
            {"kind": "type", "text": "Hello from Aidon."},
        ]))
        event.send_reply("Desktop completed" if result["ok"] else result["error"])
        server._web_runs[run_id]["status"] = "done"

    dispatcher.register("webui", handle)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler())
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with playwright.sync_playwright() as p:
            if not Path(p.chromium.executable_path).exists():
                pytest.skip("Chromium is not installed")
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                requests = []

                def approve_request(route):
                    # Inspect at request time, before the server can release
                    # the action: the modal must already be removed.
                    assert page.locator(".approval-card").count() == 0
                    requests.append(route.request.post_data_json["card_id"])
                    route.continue_()

                page.route("**/api/runs/*/approval", approve_request)
                page.goto(f"http://127.0.0.1:{httpd.server_port}/")
                page.locator("#goal-input").fill("Open gedit and type hello")
                page.locator("#send").click()
                card = page.locator(".approval-card")
                card.wait_for(state="visible")
                first = card.get_attribute("data-card-id")
                assert executed == []
                card.locator('[data-d="approved"]').click()
                page.wait_for_function(
                    "old => { const c = document.querySelector('.approval-card'); "
                    "return c && c.dataset.cardId !== old; }", arg=first)
                assert len(executed) == 1
                assert "1 tool calls" in card.inner_text()
                card.locator('[data-d="approved"]').click()
                page.get_by_text("Desktop completed", exact=True).wait_for()
                assert len(executed) == 2 and len(set(requests)) == 2
                assert not errors
            finally:
                browser.close()
    finally:
        server.webui_gate.cancel_all()
        httpd.shutdown()
        httpd.server_close()
