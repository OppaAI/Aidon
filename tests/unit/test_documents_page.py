"""WebUI Documents page: the guide exists and the chat links to it."""

from pathlib import Path

from src.interface.webui import STATIC_DIR


def test_documents_page_exists_with_guide_sections():
    page = STATIC_DIR / "docs.html"
    assert page.is_file()
    text = page.read_text(encoding="utf-8")
    assert "How to use Aidon" in text
    for section in ("Run modes", "Interview and spec sheet",
                    "Approvals and pre-approvals", "Memory",
                    "Capabilities, skills, plugins", "Safety rules"):
        assert section in text


def test_chat_nav_links_documents_route():
    index = STATIC_DIR / "index.html"
    assert 'href="/documents"' in index.read_text(encoding="utf-8")
    interface = Path(__file__).resolve().parents[2] / "src" / "interface"
    source = (interface / "webui_http.py").read_text(encoding="utf-8")
    assert "parsed.path == '/documents'" in source
    assert "_send_static('docs.html')" in source
