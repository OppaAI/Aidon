"""Aidon diagnostics: centralized logging, error records, and
the `aidon diagnostics` report.

Bounded by design. This module records what happened and
why something failed; it never feeds the planner and never
decides a repair. The loop already repairs through failure
lessons; logs stay out of that decision path until after
the freeze.

What lives where, under <home>/:
  logs/aidon.log     rotating application log (1 MB x 5)
  logs/errors.jsonl  one JSON error record per line
  trace/<run_id>.jsonl  the run's trace events (existing)
  reports/errors/<run_id>.md  the written diagnostic report

Redaction discipline, shared with the trace recorder: a
log or report never carries a secret value, a token, a
key, a password, raw environment, file contents, or a
conversation. Structured secrets (key=value, Bearer
tokens) are scrubbed by regex; everything else is kept
out by policy at the call site, not by the filter.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 5
TRACEBACK_LIMIT = 4000

_KEY_VALUE_RE = re.compile(
    r"(\b(?:api[_-]?key|apikey|access[_-]?token|client[_-]?secret|"
    r"secret|password|passwd|pwd|authorization|token)"
    r"\s*[:=]\s*\"?(?:Bearer\s+)?)[^\s\"',}\]]+",
    re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)


def redact_text(text: str) -> str:
    """Scrub structured secrets from one string. Best effort:
    an unparseable value is left alone, never expanded."""
    if not text:
        return ""
    text = _BEARER_RE.sub("Bearer ***", str(text))
    return _KEY_VALUE_RE.sub(r"\1***", text)


class RedactionFilter(logging.Filter):
    """Scrub secrets from log records before they reach a
    handler. The record's formatted message is scrubbed
    and its args cleared, so no handler sees the raw text."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact_text(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


def setup_logging(home, level: str = "INFO") -> logging.Logger:
    """Attach the rotating aidon.log handler to the "aidon"
    logger. Idempotent: a second call for the same file
    adds no handler. File-only: nothing here writes to
    the console, so user-visible output keeps coming from
    traces, reports, and replies."""
    home = Path(home)
    log_path = home / "logs" / "aidon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("aidon")
    try:
        logger.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    except Exception:
        logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in logger.handlers:
        if (isinstance(handler, RotatingFileHandler)
                and handler.baseFilename == str(log_path)):
            return logger
    handler = RotatingFileHandler(
        log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactionFilter())
    logger.addHandler(handler)
    return logger


def record_error(home, run_id: str, stage: str, message: str,
                 traceback_text: str = "") -> dict | None:
    """Append one redacted error record to logs/errors.jsonl.
    Returns the record, or None when recording itself fails.
    Never raises: diagnostics must never break the run."""
    try:
        home = Path(home)
        logs_dir = home / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "run_id": str(run_id or ""),
            "stage": str(stage or ""),
            "message": redact_text(message or ""),
            "traceback": redact_text(
                (traceback_text or "")[:TRACEBACK_LIMIT]),
        }
        path = logs_dir / "errors.jsonl"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return record
    except Exception:
        return None


def log_run_error(home, run_id: str, stage: str, message: str) -> None:
    """Best-effort pair: the errors.jsonl record plus the
    aidon.log entry (when logging was set up). Call sites
    pass messages only, never env dicts or file contents."""
    record_error(home, run_id, stage, message)
    try:
        logging.getLogger("aidon.loop").error(
            "[%s] %s: %s", str(run_id or ""), str(stage or ""),
            redact_text(message or ""))
    except Exception:
        pass


def _read_trace(home: Path, run_id: str) -> list[dict]:
    try:
        lines = (home / "trace" / f"{run_id}.jsonl").read_text(
            encoding="utf-8").splitlines()
    except Exception:
        return []
    events = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _read_errors(home: Path, run_id: str) -> list[dict]:
    try:
        lines = (home / "logs" / "errors.jsonl").read_text(
            encoding="utf-8").splitlines()
    except Exception:
        return []
    found = []
    for line in lines:
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("run_id") == run_id:
            found.append(parsed)
    return found


def _log_tail(home: Path, run_id: str, limit: int = 100) -> list[str]:
    try:
        lines = (home / "logs" / "aidon.log").read_text(
            encoding="utf-8").splitlines()
    except Exception:
        return []
    matching = [line for line in lines if run_id and run_id in line]
    if matching:
        return matching[-limit:]
    return lines[-50:]


def build_diagnostic_report(home, run_id: str) -> str:
    """Human-readable diagnostic report for one run: trace
    events, recorded errors with sanitized tracebacks, log
    tail, environment, and reproduction steps. The whole
    text is redacted once more before return."""
    home = Path(home)
    run_id = str(run_id or "")
    now = datetime.now(timezone.utc).isoformat()
    parts = [f"# Diagnostic report: {run_id}",
             "",
             f"Generated: {now}",
             f"Python: {sys.version.split()[0]}",
             f"Platform: {platform.platform()}",
             f"Home: {home}",
             "",
             "## Trace"]
    events = _read_trace(home, run_id)
    if not events:
        parts.append("No trace found for this run.")
    else:
        for event in events[-100:]:
            kind = event.get("kind", "?")
            status = event.get("status", "?")
            detail = event.get("detail", "")
            parts.append(f"- {kind} {status}: {detail}")
    parts += ["", "## Errors"]
    errors = _read_errors(home, run_id)
    if not errors:
        parts.append("No recorded errors for this run.")
    else:
        for error in errors:
            parts.append(f"- [{error.get('ts', '')}] "
                         f"{error.get('stage', '')}: "
                         f"{error.get('message', '')}")
            traceback_text = error.get("traceback", "")
            if traceback_text:
                parts.append("  ```")
                parts.append(traceback_text[:2000])
                parts.append("  ```")
    parts += ["", "## Log tail"]
    tail = _log_tail(home, run_id)
    if not tail:
        parts.append("No application log found.")
    else:
        parts.append("```")
        parts.extend(tail)
        parts.append("```")
    parts += ["",
              "## Environment",
              f"- Trace file: {home / 'trace' / (run_id + '.jsonl')}",
              f"- Errors file: {home / 'logs' / 'errors.jsonl'}",
              f"- Application log: {home / 'logs' / 'aidon.log'}",
              "",
              "## Reproduction",
              "1. Re-run the same goal in the same home.",
              "2. Compare the new trace with the trace above.",
              "3. Attach this report when asking for help; it "
              "carries no secrets by design."]
    return redact_text("\n".join(parts))


def write_diagnostic_report(home, run_id: str) -> Path:
    """Write the report to reports/errors/<run_id>.md and
    return its path."""
    home = Path(home)
    out_dir = home / "reports" / "errors"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{run_id}.md"
    path.write_text(build_diagnostic_report(home, run_id),
                    encoding="utf-8")
    return path
