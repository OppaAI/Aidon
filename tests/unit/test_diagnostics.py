"""Bounded diagnostics: redaction, rotating log setup,
error records, and the diagnostic report."""

import json
import logging
from logging.handlers import RotatingFileHandler

from src.system.diagnostics import (
    build_diagnostic_report,
    record_error,
    redact_text,
    setup_logging,
    write_diagnostic_report,
)


def _clean_aidon_logger():
    logger = logging.getLogger("aidon")
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    logger.propagate = True
    logger.setLevel(logging.WARNING)


def test_redaction_scrubs_structured_secrets():
    text = redact_text(
        'api_key=sk-secret-123 and token: abc and '
        'Authorization: Bearer xyz-999 and password="hunter2"')
    assert "sk-secret-123" not in text
    assert "xyz-999" not in text
    assert "hunter2" not in text
    assert "abc" not in text
    assert "api_key=***" in text
    assert "Authorization: Bearer ***" in text


def test_redaction_scrubs_bare_bearer_tokens():
    assert redact_text("Bearer xyz-999") == "Bearer ***"


def test_redaction_leaves_plain_text_alone():
    assert redact_text("tool failed: file not found") == \
        "tool failed: file not found"


def test_setup_logging_is_idempotent_and_rotating(tmp_path):
    try:
        first = setup_logging(tmp_path)
        second = setup_logging(tmp_path)
        assert first is second
        log_path = tmp_path / "logs" / "aidon.log"
        # Per-path idempotency: other tests may have added
        # handlers for other homes in this process, so count
        # only the handlers for this file.
        ours = [h for h in first.handlers
                if isinstance(h, RotatingFileHandler)
                and h.baseFilename == str(log_path)]
        assert len(ours) == 1
        handler = ours[0]
        assert handler.maxBytes == 1_000_000
        assert handler.backupCount == 5
        assert log_path.is_file()
        logging.getLogger("aidon.test").error("hello log")
        assert "hello log" in log_path.read_text(encoding="utf-8")
    finally:
        _clean_aidon_logger()


def _trace_and_error(home, run_id):
    trace_dir = home / "trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / f"{run_id}.jsonl").write_text(
        json.dumps({"kind": "plan", "status": "done",
                    "detail": "plan built"}) + "\n"
        + json.dumps({"kind": "sandbox", "status": "failed",
                      "detail": "tool crashed"}) + "\n",
        encoding="utf-8")
    record_error(home, run_id, "sandbox", "tool boom",
                 traceback_text="Traceback (most recent call last):\n"
                 "ValueError: boom\napi_key=sk-secret-123")


def test_report_correlates_trace_errors_and_redacts(tmp_path):
    _trace_and_error(tmp_path, "run-9")
    report = build_diagnostic_report(tmp_path, "run-9")
    assert "run-9" in report
    assert "plan done" in report
    assert "tool boom" in report
    assert "ValueError: boom" in report
    assert "sk-secret-123" not in report
    errors = (tmp_path / "logs" / "errors.jsonl").read_text(
        encoding="utf-8")
    assert "sk-secret-123" not in errors


def test_report_for_unknown_run_says_so(tmp_path):
    report = build_diagnostic_report(tmp_path, "nope")
    assert "No trace found" in report
    assert "No recorded errors" in report


def test_write_report_path(tmp_path):
    _trace_and_error(tmp_path, "run-7")
    path = write_diagnostic_report(tmp_path, "run-7")
    assert path == tmp_path / "reports" / "errors" / "run-7.md"
    assert "run-7" in path.read_text(encoding="utf-8")
