"""Clef validate gate: action mapping, thresholds, and state caps."""

from src.agentic.clef_validate import (
    MAX_STATE_CHARS, summarize_deliverables, validate_gate)


class _StubClef:
    def __init__(self, choice, confidence, error=None):
        self.choice = choice
        self.confidence = confidence
        self.error = error
        self.calls = []

    def decide_choice(self, state, name, instructions, criteria):
        self.calls.append((state, name, criteria))
        if self.error is not None:
            raise self.error
        assert self.choice in criteria
        return self.choice, self.confidence, {}


def test_accept_skips_verifier():
    result = validate_gate("goal", ["a.json"], ["a.json exists"],
                           "/tmp", _StubClef("accept", 0.9))
    assert result["action"] == "accept"


def test_accept_defers_for_content_deliverables():
    # A good-looking sample is not evidence about content.
    for deliverables in (["report.md"], ["a.json", "notes.md"],
                         ["script.py"], ["data"], ["image.png"]):
        result = validate_gate("goal", deliverables, ["x exists"],
                               "/tmp", _StubClef("accept", 0.95))
        assert result["action"] == "escalate", deliverables


def test_low_confidence_accept_defers():
    result = validate_gate("goal", ["a.txt"], ["a.txt exists"],
                           "/tmp", _StubClef("accept", 0.4))
    assert result["action"] == "escalate"


def test_retry_needs_high_confidence():
    high = validate_gate("goal", ["a.txt"], ["a.txt exists"],
                         "/tmp", _StubClef("retry", 0.9))
    assert high["action"] == "retry"
    low = validate_gate("goal", ["a.txt"], ["a.txt exists"],
                        "/tmp", _StubClef("retry", 0.7))
    assert low["action"] == "escalate"


def test_escalate_defers():
    result = validate_gate("goal", ["a.txt"], ["a.txt exists"],
                           "/tmp", _StubClef("escalate", 0.95))
    assert result["action"] == "escalate"


def test_no_client_defers():
    result = validate_gate("goal", ["a.txt"], ["a.txt exists"], "/tmp", None)
    assert result["action"] == "escalate"


def test_error_propagates_for_caller_fallback():
    try:
        validate_gate("goal", ["a.txt"], ["a.txt exists"], "/tmp",
                      _StubClef("accept", 0.9, error=RuntimeError("down")))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected transport error to propagate")


def test_summary_caps_and_marks(tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 5000)
    missing = "gone.txt"
    binary = tmp_path / "blob.bin"
    binary.write_bytes(bytes(range(256)) * 4)
    summary = summarize_deliverables(tmp_path, ["big.txt", "gone.txt",
                                                "blob.bin"])
    assert len(summary) <= MAX_STATE_CHARS + 500
    assert "[missing]" in summary
    assert "binary or opaque" in summary
    assert "xxxx" in summary
