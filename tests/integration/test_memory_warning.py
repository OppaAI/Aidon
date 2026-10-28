"""Memory fallback warning: the dashboard status must say loudly when
memory is on SQLite/Hash instead of Qdrant, and setup.sh must offer
the Qdrant server-binary install."""

from pathlib import Path
from types import SimpleNamespace

from src.interface.webui import CHAT_HTML, WebUIServer


class SQLiteStore:
    pass


class QdrantStore:
    pass


class HashEmbedder:
    pass


class EmbeddingGemmaEmbedder:
    pass


def _server(store_cls, embedder_cls):
    server = WebUIServer.__new__(WebUIServer)
    server.config = None
    server.memory = SimpleNamespace(
        _memory=SimpleNamespace(store=store_cls(), embedder=embedder_cls()))
    server.messaging_config = SimpleNamespace(
        whatsapp_configured=lambda: False, slack_configured=lambda: False)
    server.tts_key = None
    server.transcribe_key = None
    return server


def test_status_warns_on_sqlite_and_hash():
    status = _server(SQLiteStore, HashEmbedder).handle_status()
    assert "SQLite" in status["memory_warning"]
    assert "hash" in status["memory_warning"].lower()


def test_status_quiet_on_qdrant_and_real_embedder():
    status = _server(QdrantStore, EmbeddingGemmaEmbedder).handle_status()
    assert status["memory_warning"] == ""


def test_chat_page_has_system_status_section():
    assert 'id="system-status"' in CHAT_HTML
    assert "/api/status" in CHAT_HTML
    assert 'id="memory-warning"' not in CHAT_HTML


def test_setup_offers_qdrant_server_binary():
    setup = (Path(__file__).parents[2] / "setup.sh").read_text()
    assert "install_qdrant_server" in setup
    assert "aidon-qdrant.service" in setup
    assert "--with-qdrant" in setup
    assert "healthz" in setup
    assert "v1.19.1" in setup
    assert "AIDON_QDRANT_URL" in setup
