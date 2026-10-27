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
