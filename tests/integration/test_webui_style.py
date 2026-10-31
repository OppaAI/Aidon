"""WebUI pages must be styled, not plain white documents.

Regression: the chat page shipped only avatar-animation CSS and the
handler-built pages (settings, library, capabilities, memory, knowledge,
reports) shipped no <head> at all, so browsers rendered them unstyled.
"""
from src.interface.webui import (
    BASE_CSS,
    CHAT_HTML,
    DAG_HTML,
    FACTORY_HTML,
    REPLAY_HTML,
    VRM_HTML,
    _page_html,
)


def test_base_css_has_page_rules():
    # Dark theme and the low-glare olive/terracotta light theme.
    assert "--bg:#030b1c" in BASE_CSS
    assert "--accent:#0082fb" in BASE_CSS
    assert ":root{--bg:#d3d1c5;--surface:#f0ede3" in BASE_CSS
    assert "button{" in BASE_CSS
    assert "#messages p{" in BASE_CSS


def test_full_page_constants_include_base_style():
    for page in (CHAT_HTML, DAG_HTML, REPLAY_HTML, VRM_HTML, FACTORY_HTML):
        assert "<style>" in page
        assert "--bg:#030b1c" in page, page[:80]


def test_page_html_wraps_fragment():
    html = _page_html("Settings", "<h1>Settings</h1>")
    assert html.startswith("<!doctype html>")
    assert "<title>Settings</title>" in html
    assert "--bg:#030b1c" in html
    assert "<h1>Settings</h1>" in html
    assert html.endswith("</body></html>")


def test_chat_page_keeps_avatar_style():
    # The shared style is added alongside, not instead of, the avatar CSS.
    assert "robot-bob" in CHAT_HTML
    assert "render_markdown" in CHAT_HTML
