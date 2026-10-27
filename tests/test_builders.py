"""Document builder + system-audio tests."""

import csv
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.capabilities.audio import record_system_audio, vrm_lip_sync_js
from src.capabilities.office import (
    make_docx,
    make_latex,
    make_slides,
    make_spreadsheet,
)


def test_spreadsheet_contains_sheet_data(tmp_path):
    sheets = [
        {"name": "Fruit", "headers": ["item", "price"],
         "rows": [["apple", 3], ["pear", 4]]},
        {"name": "Counts", "headers": ["n"], "rows": [[1], [2]]},
    ]
    result = make_spreadsheet(sheets, tmp_path / "book.xlsx")
    if result.endswith("_manifest.txt"):
        manifest = Path(result)
        assert manifest.is_file()
        fruit = tmp_path / "book_Fruit.csv"
        assert fruit.is_file()
        with open(fruit, newline="", encoding="utf-8") as handle:
            rows = list(csv.reader(handle))
        assert rows[0] == ["item", "price"]
        assert ["apple", "3"] in rows
    else:
        import openpyxl
        wb = openpyxl.load_workbook(result)
        assert wb["Fruit"]["A2"].value == "apple"
        assert wb["Counts"]["A2"].value == 1


def test_docx_zip_contains_title_and_text(tmp_path):
    out = make_docx("My Title", [
        {"type": "heading", "text": "Section One", "level": 1},
        {"type": "paragraph", "text": "Hello document."},
        {"type": "bullet", "text": "a point"},
        {"type": "table", "headers": ["a", "b"], "rows": [["1", "2"]]},
    ], tmp_path / "doc.docx")
    assert zipfile.is_zipfile(out)
    with zipfile.ZipFile(out) as archive:
        text = archive.read("word/document.xml").decode("utf-8")
    assert "My Title" in text
    assert "Hello document." in text
    assert "Section One" in text


def test_slides_zip_contains_slide_text(tmp_path):
    out = make_slides("Deck Title", [
        {"title": "Slide One", "bullets": ["first point"]},
        {"title": "Slide Two", "bullets": ["second point"]},
    ], tmp_path / "deck.pptx")
    assert zipfile.is_zipfile(out)
    with zipfile.ZipFile(out) as archive:
        names = archive.namelist()
        joined = ""
        for name in names:
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                joined += archive.read(name).decode("utf-8")
    assert "Deck Title" in joined
    assert "Slide One" in joined
    assert "first point" in joined
    assert "Slide Two" in joined


def test_docx_and_pptx_xml_is_well_formed(tmp_path):
    import xml.etree.ElementTree as ET
    docx = make_docx("T", [{"type": "paragraph", "text": "hi"}], tmp_path / "a.docx")
    pptx = make_slides("Deck", [{"title": "S1", "bullets": ["b"]}], tmp_path / "a.pptx")
    for path in (docx, pptx):
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith((".xml", ".rels")):
                    ET.fromstring(archive.read(name))


def test_latex_contains_title_and_sections(tmp_path):
    out = make_latex("A Paper", [
        {"heading": "Intro", "body": "Some text & more.\n\n- a bullet\n- another"},
        {"heading": "End", "body": "Done."},
    ], tmp_path / "paper.tex", author="Tester")
    text = Path(out).read_text(encoding="utf-8")
    assert r"\documentclass{article}" in text
    assert "A Paper" in text
    assert r"\section{Intro}" in text
    assert r"\section{End}" in text
    assert r"\item a bullet" in text
    assert r"\&" in text  # LaTeX-escaped ampersand, not a raw one


def test_builders_reject_empty_input(tmp_path):
    with pytest.raises(ValueError):
        make_spreadsheet([], tmp_path / "x.xlsx")
    with pytest.raises(ValueError):
        make_spreadsheet([{"name": "S"}], tmp_path / "x.xlsx")
    with pytest.raises(ValueError):
        make_docx("", [{"type": "paragraph", "text": "hi"}], tmp_path / "x.docx")
    with pytest.raises(ValueError):
        make_docx("T", [], tmp_path / "x.docx")
    with pytest.raises(ValueError):
        make_slides("T", [], tmp_path / "x.pptx")
    with pytest.raises(ValueError):
        make_slides("T", [{"title": ""}], tmp_path / "x.pptx")
    with pytest.raises(ValueError):
        make_latex("T", [], tmp_path / "x.tex")
    with pytest.raises(ValueError):
        make_latex("", [{"heading": "H", "body": "b"}], tmp_path / "x.tex")


def test_record_system_audio_raises_when_no_recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg.*arecord.*sox"):
        record_system_audio(5, tmp_path / "cap.wav")
    assert not (tmp_path / "cap.wav").exists()
    with pytest.raises(ValueError):
        record_system_audio(0, tmp_path / "cap.wav")
    with pytest.raises(ValueError):
        record_system_audio(5, "")


def test_vrm_lip_sync_unchanged():
    js = vrm_lip_sync_js()
    assert "AnalyserNode" in js
    assert "Fcl_MTH" in js
    assert "Fcl_EYE_Close" in js
