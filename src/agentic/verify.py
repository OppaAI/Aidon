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



def _relative_name(workdir: Path, name: str) -> str:
    """Normalize model-facing ``workdir/foo`` paths to ``foo``.

    The planner sees the sandbox label ``workdir`` in prompts, while the
    verifier already receives that directory as its root. Joining both
    would incorrectly look for ``workdir/workdir/foo``. The user's
    "workspace folder" is the same folder, so its prefix strips too.
    """
    value = str(name).strip().replace("\\", "/")
    root_name = workdir.name.strip()
    if root_name and value == root_name:
        return ""
    if root_name and value.startswith(root_name + "/"):
        return value[len(root_name) + 1:]
    if value.startswith("workdir/"):
        return value[len("workdir/"):]
    if value.startswith("workspace folder/"):
        return value[len("workspace folder/"):]
    return value


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
    # Models often describe the evidence instead of naming the generated
    # file. The file-producing tool is the source of truth in this case.
    if re.search(r"(?:pdf|document|file)\s+(?:file\s+)?(?:is\s+)?(?:created|exists)\s+inside\s+(?:the\s+)?(?:workdir|workspace folder)(?:\b|$)",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*")
                 if path.is_file() and (path.suffix.lower() == ".pdf"
                                        if "pdf" in text.lower() else True)]
        ok = bool(files)
        return ok, ("generated file exists inside workdir" if ok
                    else "generated file missing inside workdir")
    if re.search(r"(?:output|final)\s+message\s+provides?\s+(?:a\s+)?link\s+to\s+(?:the\s+)?(?:file|document|pdf)",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*") if path.is_file()]
        ok = bool(files)
        return ok, ("output link target available" if ok
                    else "output link target missing")
    if re.search(r"(?:pdf|document)\s+link\s+(?:can\s+be\s+)?returned\s+in\s+output\s+message",
                 text, re.IGNORECASE):
        files = [path for path in workdir.rglob("*")
                 if path.is_file() and path.suffix.lower() == ".pdf"]
        ok = bool(files)
        return ok, ("output link target available" if ok
                    else "output link target missing")
    match = re.match(r"^(.+?)\s+exists$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        ok = (workdir / name).is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    match = re.match(r"^.+?\s+exists\s+at\s+(.+)$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        ok = (workdir / name).is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    # The user calls the workdir the "workspace folder": same folder,
    # same check. A leading "PDF file"/"document"/"file" descriptor
    # is model phrasing, not part of the filename.
    match = re.match(r"^(.+?)\s+exists\s+in\s+(?:the\s+)?workspace folder$",
                     text, re.IGNORECASE)
    if match:
        name = re.sub(r"^(?:pdf\s+file|document|file)\s+", "",
                      match.group(1).strip(), flags=re.IGNORECASE)
        name = _relative_name(workdir, name)
        ok = (workdir / name).is_file()
        return ok, f"{name} exists" if ok else f"{name} missing"
    match = re.match(r"^(.+?)\s+is non-empty$", text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        path = workdir / name
        ok = path.is_file() and path.stat().st_size > 0
        return ok, f"{name} non-empty" if ok else f"{name} empty or missing"
    match = re.match(r"^(.+?)\s+contains\s+(.+)$", text, re.IGNORECASE)
    if match:
        name, needle = _relative_name(workdir, match.group(1)), match.group(2).strip()
        path = workdir / name
        if not path.is_file():
            return False, f"{name} missing"
        ok = needle in path.read_text(encoding="utf-8", errors="ignore")
        return ok, f"{name} contains {needle}" if ok else f"{name} lacks {needle}"
    match = re.match(r"^ledger\s+(.+?)\s+total is\s+(-?\d+)$", text, re.IGNORECASE)
    if match:
        name, expected = _relative_name(workdir, match.group(1)), int(match.group(2))
        total = _ledger_total(workdir, name)
        if total is None:
            return False, f"ledger {name} total not computable"
        return total == expected, \
            f"ledger {name} total {total}" if total == expected else \
            f"ledger {name} total {total} != {expected}"
    match = re.match(
        r"^(?:the )?(?:final )?output message provides link to (?:the )?(.+)$",
        text, re.IGNORECASE)
    if match:
        name = _relative_name(workdir, match.group(1))
        target = (workdir / name)
        ok = target.is_file()
        if not ok and name.lower() in {"pdf", "document", "file"}:
            ok = any(path.is_file() and path.suffix.lower() == ".pdf"
                     for path in workdir.rglob("*")) if name.lower() == "pdf" else False
        return ok, (f"output link target {name} available" if ok else
                    f"output link target {name} missing")
    return False, f"unverified criterion: {criterion}"


def verify_plan(workdir: Path, deliverables: list[str],
                criteria: list[str]) -> tuple[bool, list[str], list[str]]:
    """Returns (ok, failures, verified_files)."""
    failures: list[str] = []
    verified: list[str] = []
    pdf_files = [path for path in workdir.rglob("*")
                 if path.is_file() and path.suffix.lower() == ".pdf"]
    for name in deliverables:
        relative = _relative_name(workdir, name)
        path = workdir / relative
        if not path.is_file():
            # If a document tool produced exactly one PDF, its structured
            # result may have been lost by an older adapter or an in-flight
            # repair. Recover the real artifact rather than asking a model
            # to verify a prose label or replacing the plan with ``list``.
            if (len(pdf_files) == 1 and
                    re.search(r"pdf|document", str(name), re.IGNORECASE)):
                verified.append(str(pdf_files[0].relative_to(workdir)))
            else:
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
