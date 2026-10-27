"""Chat page layout: sidebar + chat column, not a vertical stack.

Regression: the first styling pass only styled components, so the
chat page still rendered as one vertical document (avatar, then
Projects/Library/Settings/Workflows, then pills, then chat).
"""
from src.interface.webui import BASE_CSS, CHAT_HTML


def test_chat_page_has_layout_structure():
    assert '<body class="chat-page">' in CHAT_HTML
    order = ['id="layout"', 'id="sidecol"', 'id="sidebar"',
             'id="chat"', 'id="messages"', 'id="approval"']
    positions = [CHAT_HTML.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_layout_wrappers_close():
    # sidecol closes after the sidebar, layout closes after the chat.
    assert '</div>\n</div>\n<div id="chat">' in CHAT_HTML
    assert '<div id="approval"></div>\n</div>\n</div>\n<script>' in CHAT_HTML


def test_base_css_has_layout_rules():
    assert "#layout{display:flex" in BASE_CSS
    assert "#sidecol{width:280px" in BASE_CSS
    assert "#chat{flex:1" in BASE_CSS
    assert "#sidebar a{display:block" in BASE_CSS
    assert "body.chat-page{max-width:1200px}" in BASE_CSS
    assert "@media (max-width:700px)" in BASE_CSS


def test_chat_page_keeps_avatar_and_approval_js():
    assert "robot-bob" in CHAT_HTML
    assert "refreshApprovals" in CHAT_HTML
    assert "/api/approvals" in CHAT_HTML
