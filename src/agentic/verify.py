"""Verifier: evidence, not assertion.

A plan is verified by code against files in the workdir:
- every deliverable exists and is non-empty,
- every criterion is checked, not just listed,
- a ledger file's stated total is recomputed from its
  lines, in cents, and a mismatch is a failure, even if
  the file exists.

Criteria, simple and checkable, in plain words:
- "X exists" -> file X exists,
- "X is non-empty" -> file X exists and is non-empty,
- "X contains Y" -> file X text contains Y,
- "ledger X total is N" -> recomputed total for X equals N,
  where N is in cents for a JSON ledger, or in the file's
  own units for a text ledger.
Anything else is not a checkable criterion: it is
reported as unverified, not passed, so a model cannot
pass itself by writing a vague criterion.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..capabilities.tools import ToolRegistry


def _ledger_total(workdir: Path, name: str) -> int | None:
    target = workdir / name
    if not target.is_file():
        return None
    raw = target.read_text(encoding="utf-8", errors="ignore")
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and "lines" in data:
            return sum(int(line.get("amount_cents", 0))
                       for line in data["lines"])
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        pass
    try:
        return sum(int(line.strip()) for line in raw.splitlines()
                   if line.strip())
    except ValueError:
        return None


def check_criterion(workdir: Path, criterion: str) -> tuple[bool, str]:
    text = str(criterion).strip()
    match = re.match(r"^(.+?)\s+exists$", text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        ok = (workdir / name).is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    match = re.match(r"^(.+?)\s+is non-empty$", text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        path = workdir / name
        ok = path.is_file() and path.stat().st_size > 0
        return ok, f"{name} non-empty" if ok else f"{name} empty or missing"
    match = re.match(r"^(.+?)\s+contains\s+(.+)$", text, re.IGNORECASE)
    if match:
        name, needle = match.group(1).strip(), match.group(2).strip()
        path = workdir / name
        if not path.is_file():
            return False, f"{name} missing"
        ok = needle in path.read_text(encoding="utf-8", errors="ignore")
        return ok, f"{name} contains {needle}" if ok else f"{name} lacks {needle}"
    match = re.match(r"^ledger\s+(.+?)\s+total is\s+(-?\d+)$", text, re.IGNORECASE)
    if match:
        name, expected = match.group(1).strip(), int(match.group(2))
        total = _ledger_total(workdir, name)
        if total is None:
            return False, f"ledger {name} total not computable"
        return total == expected, \
            f"ledger {name} total {total}" if total == expected else \
            f"ledger {name} total {total} != {expected}"
    return False, f"unverified criterion: {criterion}"


def verify_plan(workdir: Path, deliverables: list[str],
                criteria: list[str]) -> tuple[bool, list[str], list[str]]:
    """Returns (ok, failures, verified_files)."""
    failures: list[str] = []
    verified: list[str] = []
    for name in deliverables:
        path = workdir / name
        if not path.is_file():
            failures.append(f"missing deliverable: {name}")
        elif path.stat().st_size == 0:
            failures.append(f"empty deliverable: {name}")
        else:
            verified.append(name)
    for criterion in criteria:
        ok, message = check_criterion(workdir, criterion)
        if not ok:
            failures.append(message)
    return (not failures, failures, verified)
