"""Old-Aidon theme + real settings page.

Regression: the settings page rendered only a header and the API-keys
list, so it looked empty and gave no way to change a setting. The
catalog and the .env writer existed as APIs but no page used them.
"""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from src.interface.webui import (
    BASE_CSS,
    CHAT_HTML,
    WebUIServer,
    _page_html,
)


def test_theme_variables_and_toggle():
    assert "--bg:#070b16" in BASE_CSS  # old Aidon dark default
    assert "--accent:#0082fb" in BASE_CSS
    assert "body.light{--bg:#eef1f7" in BASE_CSS  # light override
    assert 'id="theme-toggle"' in CHAT_HTML
    assert "aidon-theme" in CHAT_HTML
    assert "aidon-theme" in _page_html("X", "<p>y</p>")


def test_settings_page_has_form():
    # Drive the handler's settings branch through a live server.
    with TemporaryDirectory() as td:
        home = Path(td)
        srv = WebUIServer(home, None, None)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler())
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{httpd.server_address[1]}"
            with urllib.request.urlopen(base + "/settings") as resp:
                page = resp.read().decode()
            assert 'id="settings-groups"' in page
            assert 'id="settings-save"' in page
            assert "Keys, hash prefixes only" in page
            # The page itself is static now; the form's wiring lives in
            # its script, so the catalog call is asserted there.
            with urllib.request.urlopen(base + "/static/settings.js") as resp:
                script = resp.read().decode()
            assert "/api/settings/catalog" in script
            assert "/api/settings" in script
            assert "restart aidon webui" in (page + script).lower()
        finally:
            httpd.shutdown()

        # The env writer behind the form: non-secret write lands in .env.
        result = srv.settings_set_env("AIDON_TIMEZONE", "America/Vancouver")
        assert result["ok"] is True
        assert "AIDON_TIMEZONE=America/Vancouver" in (home / ".env").read_text()
        catalog = srv.settings_catalog()
        tz = [s for g in catalog["groups"] for s in g["settings"]
              if s["key"] == "AIDON_TIMEZONE"]
        assert tz and tz[0]["value"] == "America/Vancouver"
        # Secrets are writable here, old-Aidon behaviour: write-only,
        # never shown back, empty leaves the current value alone.
        secret = srv.settings_set_env("AIDON_API_KEY", "sk-test-value")
        assert secret["ok"] is True
        assert "sk-test-value" not in str(secret)
        assert "AIDON_API_KEY=sk-test-value" in (home / ".env").read_text()
        catalog = srv.settings_catalog()
        key_entry = [s for g in catalog["groups"] for s in g["settings"]
                     if s["key"] == "AIDON_API_KEY"]
        assert key_entry and key_entry[0]["value"] == "" and key_entry[0]["is_set"] is True
        kept = srv.settings_set_env("AIDON_API_KEY", "")
        assert kept == {"ok": True, "key": "AIDON_API_KEY", "unchanged": True}
