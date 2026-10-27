"""Mail rules and corrected-subject bookkeeping, pure code, no sending.

A rule is a dict with match fields, from_contains, subject_contains, body_contains, and an
action, draft, archive, label, or ignore. apply_rules(message, rules) returns the first
matching rule's action, or ignore if none match, so a caller can decide what to do with a
message without the model deciding. Matching is case-insensitive substring, empty match
fields are ignored, not treated as matching everything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

_PREFIX_RE = re.compile(r"^Corrected:\s*(\[[^\]]*\]\s*)?", re.IGNORECASE)
_RECIPIENT_KEYS = ("to", "recipient", "recipients", "to_email", "email_to")
_ATTACHMENT_KEYS = ("attachments", "attachment", "files", "file_paths", "attachment_paths", "attach")
_BODY_KEYS = ("body", "text", "message", "html", "content")
_MAX_ENTRIES = 200


def enabled() -> bool:
    return os.environ.get("AIDON_CORRECTED_SUBJECT", "1") != "0"


def base_subject(subject: str) -> str:
    return _PREFIX_RE.sub("", subject or "").strip()


def corrected_subject(subject: str, now: datetime) -> str:
    return f"Corrected: [{now.strftime('%Y-%m-%d %H:%M')}] {base_subject(subject)}"


def is_email_send(arguments: dict) -> bool:
    return (isinstance(arguments, dict)
            and isinstance(arguments.get("subject"), str)
            and bool(arguments.get("subject", "").strip())
            and any(arguments.get(k) for k in _RECIPIENT_KEYS))


def _paths_from(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_paths_from(item))
        return out
    if isinstance(value, dict):
        for key in ("path", "file", "filepath", "filename"):
            if isinstance(value.get(key), str):
                return [value[key]]
    return []


def attachment_paths(arguments: dict) -> list[str]:
    out = []
    for key in _ATTACHMENT_KEYS:
        out.extend(_paths_from(arguments.get(key)))
    return out


def _body_text(arguments: dict) -> str:
    for key in _BODY_KEYS:
        if isinstance(arguments.get(key), str):
            return arguments[key]
    return ""


def _file_sha(path: str) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


class SentMailLog:
    """Hash-only send log used to detect changed bodies or attachments."""

    def __init__(self, path):
        self.path = Path(path)

    def _entries(self) -> list[dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (OSError, ValueError):
            return []

    def _fingerprint(self, arguments: dict) -> dict:
        return {
            "subject_base": base_subject(str(arguments.get("subject", ""))),
            "body_sha": hashlib.sha256(_body_text(arguments).encode()).hexdigest(),
            "files": [{"name": Path(path).name, "sha": _file_sha(path)}
                      for path in attachment_paths(arguments)],
        }

    def is_correction(self, arguments: dict) -> bool:
        current = self._fingerprint(arguments)
        for entry in self._entries():
            old_files = {item.get("name"): item.get("sha")
                         for item in entry.get("files", [])}
            new_files = {item["name"]: item["sha"] for item in current["files"]}
            if any(name in old_files and old_files[name] != sha
                   for name, sha in new_files.items()):
                return True
            if entry.get("subject_base") == current["subject_base"] and (
                    entry.get("body_sha") != current["body_sha"]
                    or set(old_files) != set(new_files)):
                return True
        return False

    def rewrite_subject(self, arguments: dict, now: datetime | None = None) -> dict | None:
        if not enabled() or not is_email_send(arguments) or not self.is_correction(arguments):
            return None
        out = dict(arguments)
        out["subject"] = corrected_subject(str(arguments["subject"]), now or datetime.now())
        return out

    def record(self, arguments: dict) -> None:
        entry = self._fingerprint(arguments)
        entry["ts"] = datetime.now().isoformat(timespec="seconds")
        entries = (self._entries() + [entry])[-_MAX_ENTRIES:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(entries, indent=1), encoding="utf-8")
        except OSError:
            pass


def rule_matches(rule: dict, message: dict) -> bool:
    checks = []
    if rule.get("from_contains"):
        checks.append(rule["from_contains"].lower() in str(message.get("from", "")).lower())
    if rule.get("subject_contains"):
        checks.append(rule["subject_contains"].lower() in str(message.get("subject", "")).lower())
    if rule.get("body_contains"):
        checks.append(rule["body_contains"].lower() in str(message.get("body", "")).lower())
    if not checks:
        return False
    return all(checks)


def apply_rules(message: dict, rules: list[dict]) -> dict:
    for rule in rules:
        if rule_matches(rule, message):
            return {"matched": True, "rule": rule.get("name", ""), "action": rule.get("action", "ignore")}
    return {"matched": False, "rule": "", "action": "ignore"}
