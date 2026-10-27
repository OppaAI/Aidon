"""Truth grading for a run report, by code, not by looking.

A run is graded against criteria, which are plain strings
the deliverables are expected to contain. For each criterion,
grading checks, case-insensitively, whether that string
appears as a substring in any file content supplied. It
does not use a model, it does not guess, and it does not
grade quality, style, or completeness beyond the criteria
given. A criterion that is not a substring is marked not
found, even if a file is about the same topic in other
words, because substring grading is exact and checkable.

Functions:
- grade(report_files, criteria): report_files is a dict
  mapping a file name to its text content, criteria is a
  list of strings. Returns passed, total, score, and a
  per-criterion found mapping. Score is passed / total,
  or 0 when there are no criteria, never a division error.
- grade_from_directory(path, criteria): reads every file
  under the directory, recursively, as text, ignoring
  undecodable bytes, then calls grade(). A missing
  directory raises, an empty directory grades against
  no files, so every criterion is not found.

Limits: substring grading cannot tell whether a number
is correct, only whether it appears. For exact numbers,
use a code validator for the specific format, not this
module, and for a chart, check the data in the SVG text,
not the rendered pixels.
"""

from __future__ import annotations

from pathlib import Path


def grade(report_files: dict[str, str], criteria: list[str]) -> dict:
    """Grade file contents against criteria, by substring match."""
    files = report_files or {}
    criteria = list(criteria or [])
    contents = [str(content).lower() for content in files.values()]
    found: dict[str, bool] = {}
    for criterion in criteria:
        needle = str(criterion).lower()
        found[criterion] = bool(needle) and any(needle in content for content in contents)
    passed = sum(1 for value in found.values() if value)
    total = len(criteria)
    score = (passed / total) if total else 0.0
    return {
        "passed": passed,
        "total": total,
        "score": score,
        "found": found,
        "details": [{"criterion": criterion, "found": found[criterion]} for criterion in criteria],
    }


def grade_from_directory(path, criteria: list[str]) -> dict:
    """Grade every file under a directory against criteria."""
    directory = Path(path)
    if not directory.is_dir():
        raise ValueError(f"grade directory does not exist: {directory}")
    report_files: dict[str, str] = {}
    for file_path in sorted(directory.rglob("*")):
        if file_path.is_file():
            try:
                report_files[str(file_path)] = file_path.read_text(errors="ignore")
            except OSError:
                continue
    return grade(report_files, criteria)
