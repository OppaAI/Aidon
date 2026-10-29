"""Idle self-improvement cycle over the skill library.

The cycle does no model work and needs no model at all:
1. Re-run every candidate skill's own tests in the sandbox
   and auto-promote the ones that pass.
2. Consolidate near-duplicate validated skills: skills whose
   keyword signatures match are the same skill twice, so the
   older is retired and the newer kept.
3. Trusted duplicates are never auto-retired. Trusted is
   the owner-approved library, so duplicates there are
   reported for human review instead.

The cycle degrades loudly and never dies: a missing
sandbox, an unusable library, or one skill's test error
is recorded in the summary's skipped list, and the rest
of the cycle still runs. Returns a summary dict with
promoted, retired_duplicates, trusted_duplicates_for_review,
and skipped.
"""

from __future__ import annotations

import re

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on",
    "with", "by", "at", "from", "into", "make", "create", "write",
})


def _keywords(text: str) -> frozenset:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return frozenset(word for word in words if word not in _STOPWORDS and len(word) > 1)


def _signature(skill) -> frozenset:
    # Description and capabilities only, not the name: two skills
    # with different names and the same description are the same
    # skill twice, and a name alone must not hide that.
    haystack = " ".join([skill.description] + list(skill.capabilities or []))
    return _keywords(haystack)


def _consolidate(library, skills: list, auto_retire: bool, survivor_note: str):
    """Group skills by keyword signature.

    Returns (retired, reported): with auto_retire, all but the
    newest in each group are retired; without it, groups of
    more than one are reported as "a ~ b" strings and nothing
    is retired. Newest means highest version, then latest
    in the library's order.
    """
    groups: dict[frozenset, list] = {}
    for skill in skills:
        groups.setdefault(_signature(skill), []).append(skill)
    retired: list[str] = []
    reported: list[str] = []
    for signature, group in groups.items():
        if len(group) < 2 or not signature:
            continue
        ordered = sorted(group, key=lambda skill: (skill.version, skill.seq))
        keeper = ordered[-1]
        for old in ordered[:-1]:
            if auto_retire:
                library.retire(old.name, f"consolidated into {keeper.name} (learn cycle)")
                retired.append(f"{old.name} -> {keeper.name}")
            else:
                reported.append(f"{old.name} ~ {keeper.name}")
    return retired, reported


def run_learn_cycle(home, sandbox=None, governor_check=None, max_per_day: int = 5) -> dict:
    """Run one idle learn cycle. See the module docstring."""
    summary: dict = {
        "promoted": [],
        "retired_duplicates": [],
        "trusted_duplicates_for_review": [],
        "skipped": [],
    }
    if sandbox is None:
        summary["skipped"].append("sandbox unavailable: no skill tests run, nothing promoted")
        return summary
    try:
        from ..capabilities.skill_library import SkillLibrary
        library = SkillLibrary(home)
    except Exception as exc:
        summary["skipped"].append(f"skill library unavailable: {type(exc).__name__}: {exc}")
        return summary

    def _governor_allows(name: str) -> bool:
        if governor_check is None:
            return True
        try:
            return bool(governor_check(name, max_per_day))
        except TypeError:
            return bool(governor_check(name))

    for skill in library.list(states=("candidate",)):
        if len(summary["promoted"]) >= max_per_day:
            summary["skipped"].append(
                f"daily promotion cap reached ({max_per_day}): {skill.name} not tested")
            continue
        if not _governor_allows(skill.name):
            summary["skipped"].append(f"governor blocked: {skill.name}")
            continue
        try:
            report = library.run_tests(skill.name, sandbox)
        except Exception as exc:
            summary["skipped"].append(
                f"tests errored for {skill.name}: {type(exc).__name__}: {exc}")
            continue
        if not report["passed"]:
            summary["skipped"].append(f"tests did not pass: {skill.name}")
            continue
        try:
            library.promote(skill.name, report)
            summary["promoted"].append(skill.name)
        except Exception as exc:
            summary["skipped"].append(
                f"promotion failed for {skill.name}: {type(exc).__name__}: {exc}")

    try:
        validated = library.list(states=("validated",))
        retired, _ = _consolidate(library, validated, auto_retire=True,
                                  survivor_note="learn cycle")
        summary["retired_duplicates"].extend(retired)
    except Exception as exc:
        summary["skipped"].append(
            f"validated consolidation failed: {type(exc).__name__}: {exc}")

    try:
        trusted = library.list(states=("trusted",))
        _, reported = _consolidate(library, trusted, auto_retire=False,
                                   survivor_note="learn cycle")
        summary["trusted_duplicates_for_review"].extend(reported)
    except Exception as exc:
        summary["skipped"].append(
            f"trusted duplicate check failed: {type(exc).__name__}: {exc}")

    return summary
