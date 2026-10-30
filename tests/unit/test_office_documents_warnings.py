"""Degradation warnings: a requested format that cannot be produced,
and an OCR path that cannot run, must say so instead of silently
returning something else."""

import sys
import types
from types import SimpleNamespace

import pytest

from src.capabilities import documents as _docs
from src.capabilities.office import make_spreadsheet


def test_spreadsheet_fallback_warns_and_returns_manifest(tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "openpyxl", None)
    sheets = [{"name": "s", "headers": ["a"], "rows": [[1]]}]
    out = tmp_path / "book.xlsx"
    result = make_spreadsheet(sheets, out)
    assert result.endswith("_manifest.txt")
    assert (tmp_path / "book_s.csv").exists()
    assert (tmp_path / "book_manifest.txt").exists()
    assert "openpyxl is not installed" in capsys.readouterr().err


def test_ocr_image_returns_none_without_binary(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert _docs._ocr_image(_docs.Path("/tmp/x.png")) is None


def test_ocr_image_returns_none_without_library(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    fake_pil = types.ModuleType("PIL")
    fake_image_mod = types.ModuleType("PIL.Image")
    fake_image_mod.open = lambda path: object()
    fake_pil.Image = fake_image_mod
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_mod)
    monkeypatch.setitem(sys.modules, "pytesseract", None)
    assert _docs._ocr_image(_docs.Path("/tmp/x.png")) is None


def test_read_document_marks_ocr_unavailable(tmp_path, monkeypatch):
    class _FakeMarkItDown:
        def convert(self, path):
            return SimpleNamespace(text_content="")

    monkeypatch.setattr(_docs, "_MARKITDOWN_AVAILABLE", True)
    monkeypatch.setattr(_docs, "MarkItDown", _FakeMarkItDown, raising=False)
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = _docs.read_document(str(png))
    assert "OCR unavailable" in result


def test_read_document_marks_ocr_decode_failure(tmp_path, monkeypatch):
    class _FakeMarkItDown:
        def convert(self, path):
            return SimpleNamespace(text_content="")

    monkeypatch.setattr(_docs, "_MARKITDOWN_AVAILABLE", True)
    monkeypatch.setattr(_docs, "MarkItDown", _FakeMarkItDown, raising=False)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tesseract")
    fake_pil = types.ModuleType("PIL")
    fake_image_mod = types.ModuleType("PIL.Image")

    def _broken_open(path):
        raise OSError("damaged image")

    fake_image_mod.open = _broken_open
    fake_pil.Image = fake_image_mod
    monkeypatch.setitem(sys.modules, "PIL", fake_pil)
    monkeypatch.setitem(sys.modules, "PIL.Image", fake_image_mod)
    fake_tess = types.ModuleType("pytesseract")
    fake_tess.image_to_string = lambda img: ""
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tess)
    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    result = _docs.read_document(str(png))
    assert "READ_FAILED" in result
    assert "OCR unavailable" not in result
