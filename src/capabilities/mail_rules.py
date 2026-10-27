"""Mail rules, old Aidon mail_rules concept, rebuilt, pure code, no sending.

A rule is a dict with match fields, from_contains, subject_contains, body_contains, and an
action, draft, archive, label, or ignore. apply_rules(message, rules) returns the first
matching rule's action, or ignore if none match, so a caller can decide what to do with a
message without the model deciding. Matching is case-insensitive substring, empty match
fields are ignored, not treated as matching everything.
"""

from __future__ import annotations


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
