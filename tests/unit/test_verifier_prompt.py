"""Verifier prompt: criteria, completeness, disclosed truncation.

The old prompt gave the model the goal and file contents only, so
it judged a loose goal match: a thin or placeholder deliverable
passed, and content past 2000 chars was silently unseen. The new
prompt receives the plan criteria, defines complete, shows file
sizes, and discloses truncation, with an unconfirmable criterion
failing closed to NO.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.verifier_model import ModelVerifier


class _Rec:
    def __init__(self, reply):
        self.reply = reply
        self.messages = None

    def chat(self, messages, sampling=None):
        self.messages = messages
        return self.reply


def _prompt_of(rec):
    return rec.messages[1].content


def test_verifier_prompt_carries_criteria_sizes_and_truncation(tmp_path):
    (tmp_path / "a.md").write_text("x" * 3000)
    (tmp_path / "b.md").write_text("short body")
    rec = _Rec("YES")
    out = ModelVerifier(rec).verify(
        "Write a report", ["a.md", "b.md"], tmp_path,
        criteria=["report covers sales", "report has a total"])
    assert out["verdict"] == "yes"
    prompt = _prompt_of(rec)
    assert "- report covers sales" in prompt
    assert "- report has a total" in prompt
    assert "3000 bytes" in prompt
    assert "content truncated to 2000 chars" in prompt
    assert "no placeholders" in prompt
    assert "cannot be confirmed from the shown content, answer NO" in prompt
    assert rec.messages[0].content.startswith(
        "You verify deliverables against explicit criteria")


def test_verifier_reads_head_only(tmp_path):
    # P2 pin: a large deliverable is shown from its first 2000
    # chars only, so a tail marker must never reach the prompt.
    body = "head-" + "x" * 5000 + "-TAIL-MARKER-" + "y" * 100000
    (tmp_path / "big.md").write_text(body)
    rec = _Rec("YES")
    ModelVerifier(rec).verify("Write a report", ["big.md"], tmp_path)
    prompt = _prompt_of(rec)
    assert "content truncated to 2000 chars" in prompt
    assert "-TAIL-MARKER-" not in prompt


def test_verifier_prompt_without_criteria_still_works(tmp_path):
    (tmp_path / "a.md").write_text("body")
    rec = _Rec("NO: a.md is incomplete")
    out = ModelVerifier(rec).verify("Write a report", ["a.md"], tmp_path)
    assert out["verdict"] == "no"
    assert "none stated; judge against the goal" in _prompt_of(rec)


def test_verifier_missing_file_still_reported(tmp_path):
    rec = _Rec("NO: missing")
    out = ModelVerifier(rec).verify(
        "Write a report", ["gone.md"], tmp_path, criteria=["report exists"])
    assert out["verdict"] == "no"
    assert "(missing)" in _prompt_of(rec)
