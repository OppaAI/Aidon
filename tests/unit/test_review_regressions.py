"""Regression cases found in the repository review; no live services."""

import io
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from src.capabilities import documents, github_tools
from src.memory.embedder import HashEmbedder
from src.memory.stores import _sparse_from_text


def test_sparse_hash_collisions_produce_unique_indices_without_losing_weight():
    # These distinct tokens hash to bucket 67399. Qdrant rejects duplicates.
    text = "item111 item130 item111 dinner"
    indices, weights = HashEmbedder().embed_sparse(text)
    assert len(indices) == len(set(indices))
    assert weights[indices.index(67399)] == 3.0
    assert sum(weights) == 4.0
    assert _sparse_from_text(text) == (indices, weights)


def test_github_fetch_encodes_filename_and_ref_as_data():
    calls = []

    def get(url, token):
        calls.append(url)
        return b"source text", {}

    result = github_tools.fetch_file(
        "OppaAI", "Aidon", "demo/menu #1?.html", ref="demo/fusion&budget#2", get=get)
    assert result == {"ok": True, "text": "source text"}
    parsed = urlsplit(calls[0])
    assert unquote(parsed.path) == "/repos/OppaAI/Aidon/contents/demo/menu #1?.html"
    assert parse_qs(parsed.query) == {"ref": ["demo/fusion&budget#2"]}
    assert not parsed.fragment
    assert " " not in calls[0]


@pytest.mark.parametrize("interrupted", [False, True])
def test_document_download_failure_removes_partial_file(tmp_path, monkeypatch, interrupted):
    download = tmp_path / "download"
    download.mkdir()
    monkeypatch.setattr(documents.tempfile, "mkdtemp", lambda **kwargs: str(download))
    monkeypatch.setattr("src.agentic.research_allow.guard_reason", lambda *args: None)
    monkeypatch.setattr(documents, "MAX_DOWNLOAD_BYTES", 16)

    class Response(io.BytesIO):
        def read(self, size=-1):
            if interrupted and self.tell():
                raise OSError("connection interrupted")
            return super().read(8 if interrupted else size)

    opener = SimpleNamespace(open=lambda *args, **kwargs: Response(b"x" * 32))
    monkeypatch.setattr(documents.urllib.request, "build_opener", lambda *args: opener)
    path, error = documents._resolve_url("https://example.com/menu.pdf")
    assert path is None
    assert "READ_FAILED:" in error
    assert ("interrupted" if interrupted else "cap") in error
    assert not download.exists()


@pytest.mark.parametrize("parser_missing", [False, True])
def test_document_fallback_cleans_download_even_without_parser(tmp_path, monkeypatch, parser_missing):
    download = tmp_path / "download"
    download.mkdir()
    path = download / "menu.pdf"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(documents, "_MARKITDOWN_AVAILABLE", False)
    monkeypatch.setattr(documents, "_resolve_url", lambda *args: (path, ""))
    monkeypatch.setattr("src.memory.kb.parser_needed_for",
                        lambda path: "pypdf" if parser_missing else None)
    monkeypatch.setattr("src.memory.kb.extract_text", lambda path: "Dinner menu")
    result = documents.read_document("https://example.com/menu.pdf")
    assert ("needs parser" in result) if parser_missing else result == "Dinner menu"
    assert not download.exists()
