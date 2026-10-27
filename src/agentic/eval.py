"""Eval, old Aidon eval concept, rebuilt, code only, separate from metrics truth grading.

eval_run checks a run's deliverables against criteria, by code: a criterion passes if its
text appears, case-insensitive, in any deliverable file's content, or if the criterion
names a file that exists in the workdir. No model judgement here, a pass is a measured
pass, a fail is a measured fail, and an empty criteria list is a failure, not a pass.
"""

from __future__ import annotations

from pathlib import Path


def eval_run(criteria: list, files: dict, workdir=None) -> dict:
    if not criteria:
        return {"ok": False, "passed": [], "failed": ["no criteria"], "score": 0.0}
    passed, failed = [], []
    content = " ".join(str(v) for v in files.values()).lower()
    for criterion in criteria:
        text = str(criterion)
        if text.lower() in content:
            passed.append(text)
        elif workdir and (Path(workdir) / text).exists():
            passed.append(text)
        elif text in files:
            passed.append(text)
        else:
            failed.append(text)
    score = len(passed) / len(criteria) if criteria else 0.0
    return {"ok": not failed, "passed": passed, "failed": failed, "score": score}


# -- per-criterion evaluation with evidence, and a run harness ---------------

from dataclasses import dataclass


@dataclass
class EvalResult:
    criterion: str
    passed: bool
    evidence: str = ""

    def to_dict(self) -> dict:
        return {"criterion": self.criterion, "passed": self.passed,
                "evidence": self.evidence}


def _snippet(text: str, needle: str, radius: int = 60) -> str:
    low = text.lower()
    at = low.find(needle.lower())
    if at < 0:
        return ""
    start = max(0, at - radius)
    end = min(len(text), at + len(needle) + radius)
    snippet = text[start:end].replace("\n", " ").strip()
    return ("..." if start > 0 else "") + snippet + ("..." if end < len(text) else "")


def evaluate(criteria: list, evidence, files: dict | None = None) -> dict:
    """Per-criterion pass/fail with an evidence snippet. Evidence
    may be a string or a dict of file contents; files adds more
    content to search. A criterion passes when its text appears,
    case-insensitive, in the combined content, or when it names a
    file key. An empty criteria list is a failure, not a pass."""
    if not criteria:
        return {"ok": False, "results": [], "passed": [], "failed": ["no criteria"],
                "score": 0.0}
    chunks: list[str] = []
    if isinstance(evidence, dict):
        chunks.extend(str(v) for v in evidence.values())
    elif evidence:
        chunks.append(str(evidence))
    if files:
        chunks.extend(str(v) for v in files.values())
    content = "\n".join(chunks)
    file_keys = set()
    if isinstance(evidence, dict):
        file_keys.update(evidence.keys())
    if files:
        file_keys.update(files.keys())
    results: list[EvalResult] = []
    for criterion in criteria:
        text = str(criterion)
        if text in file_keys:
            results.append(EvalResult(text, True, f"file present: {text}"))
        elif text.lower() in content.lower():
            results.append(EvalResult(text, True, _snippet(content, text)))
        else:
            results.append(EvalResult(text, False, ""))
    passed = [r.criterion for r in results if r.passed]
    failed = [r.criterion for r in results if not r.passed]
    return {"ok": not failed,
            "results": results, "passed": passed, "failed": failed,
            "score": len(passed) / len(results) if results else 0.0}


def grade_report(evaluation) -> dict:
    """A one-line summary of an evaluate() output, or of a list of
    EvalResult objects."""
    if isinstance(evaluation, dict):
        results = evaluation.get("results", [])
    else:
        results = list(evaluation)
    passed = [r for r in results if getattr(r, "passed", False)]
    failed = [r for r in results if not getattr(r, "passed", False)]
    total = len(results)
    return {"total": total, "passed": len(passed), "failed": len(failed),
            "score": len(passed) / total if total else 0.0,
            "ok": total > 0 and not failed}


class EvalHarness:
    """Run evaluate() over a list of runs and aggregate the scores.

    A run is a dict with criteria, evidence, and optional files,
    or a (criteria, evidence) tuple. Each run's full evaluate()
    output is kept, so the aggregate never hides which run failed
    on which criterion."""

    def __init__(self, runs=None):
        self.runs = list(runs or [])

    def run(self, runs=None) -> dict:
        runs = list(runs) if runs is not None else self.runs
        per_run = []
        total = 0
        passed = 0
        for entry in runs:
            if isinstance(entry, dict):
                result = evaluate(entry.get("criteria", []),
                                  entry.get("evidence", ""),
                                  entry.get("files"))
            else:
                criteria, evidence = entry[0], entry[1]
                files = entry[2] if len(entry) > 2 else None
                result = evaluate(criteria, evidence, files)
            per_run.append(result)
            total += len(result["results"])
            passed += len(result["passed"])
        return {"runs": per_run, "total": total, "passed": passed,
                "failed": total - passed,
                "score": passed / total if total else 0.0,
                "ok": total > 0 and passed == total}
