"""Cross-file validator, stdlib only.

One file alone can look right while disagreeing with
another file: a menu can promise a dish the ledger
never bought, or a report can quote a total the ledger
does not contain. This validator checks that agreement
directly. A rule is (source_file, target_file, tokens):
it passes only if every token that appears in the
source content also appears in the target content,
case-insensitively. Tokens are checked as substrings,
not as whole words, so "roast beef" matches a line
that says "Roast Beef, 1 kg".

A missing file is a failure, not a crash: every token
in a rule with a missing source or target is reported
with ok=False, so the caller sees which check failed
and why, instead of an exception hiding the rest of
the report. The result carries both boolean and count
forms, so callers can use whichever fits: passed is
True only when every detail passed, failed is its
inverse, and passed_count / failed_count are the
detail counts.
"""

from __future__ import annotations


def cross_validate(files: dict, rules: list) -> dict:
    details = []
    for rule in rules:
        if len(rule) != 3:
            raise ValueError("each rule must be (source_file, target_file, tokens)")
        source_file, target_file, tokens = rule
        source = files.get(source_file)
        target = files.get(target_file)
        for token in tokens:
            if source is None or target is None:
                ok = False
            else:
                ok = str(token).lower() in str(target).lower()
            details.append({
                "rule": [source_file, target_file],
                "source": source_file,
                "target": target_file,
                "token": token,
                "ok": ok,
            })
    passed_count = sum(1 for detail in details if detail["ok"])
    failed_count = len(details) - passed_count
    return {
        "passed": failed_count == 0,
        "failed": failed_count > 0,
        "passed_count": passed_count,
        "failed_count": failed_count,
        "details": details,
    }
