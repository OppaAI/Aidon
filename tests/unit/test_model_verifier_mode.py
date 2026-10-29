"""Model verifier mode: tunable, default lowest (off).

AIDON_MODEL_VERIFIER off/selective/always. Off spends no model call;
selective skips only all-structured runs, where code checks are
complete; always is the previous behavior. A NO reason must name the
failed deliverable, so gradual repair can target it.
"""

import pytest

from src.agentic.loop import _model_verifier_wanted, _resolve_model_verifier_mode
from src.agentic.verifier_model import ModelVerifier
from src.system.model_client import FakeClient


def test_mode_defaults_to_off(monkeypatch):
    monkeypatch.delenv("AIDON_MODEL_VERIFIER", raising=False)
    assert _resolve_model_verifier_mode() == "off"


@pytest.mark.parametrize("raw,expected", [
    ("selective", "selective"),
    ("always", "always"),
    ("SELECTIVE", "selective"),
    ("  always  ", "always"),
    ("bogus", "off"),
    ("", "off"),
])
def test_mode_values(monkeypatch, raw, expected):
    monkeypatch.setenv("AIDON_MODEL_VERIFIER", raw)
    assert _resolve_model_verifier_mode() == expected


def test_wanted_off_never():
    assert _model_verifier_wanted("off", ["report.pdf"]) is False


def test_wanted_always():
    assert _model_verifier_wanted("always", ["data.json"]) is True


def test_wanted_selective_skips_all_structured():
    assert _model_verifier_wanted("selective", ["ledger.json", "rows.csv"]) is False
    assert _model_verifier_wanted("selective", ["rows.tsv", "dump.jsonl"]) is False


def test_wanted_selective_runs_for_prose_or_mixed():
    assert _model_verifier_wanted("selective", ["report.pdf"]) is True
    assert _model_verifier_wanted("selective", ["data.json", "notes.md"]) is True


def test_no_prompt_requires_naming_failed_files(tmp_path):
    wd = tmp_path / "workdir"
    wd.mkdir()
    (wd / "letter.txt").write_text("Dear Oppa.", encoding="utf-8")
    client = FakeClient("YES fine")
    ModelVerifier(client).verify("goal", ["letter.txt"], wd, ["letter exists"])
    prompt = client.calls[0][1].content
    assert "name the deliverable file(s) that fail" in prompt
