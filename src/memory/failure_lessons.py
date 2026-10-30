"""Failure lessons: what failed, what fixed it, and whether
it failed again.

The experience store already records failed examples
(goal, shape, error, failed tools, failed deliverables)
and recalls them by shape. What it does not keep is
everything GPT's failure-memory item asks for, and
everything a future builder needs before retrying a
similar build:

- the repair that fixed it, and the tests added with it
- the dependency combination and capability version that
  were in play when it failed
- whether the same failure recurred, and how often
- a lesson record that can be updated in place when the
  fix lands (vector points are append-only history; a
  lesson is a living record)

A lesson is therefore a structured JSON record, not
another vector point:

  goal, goal_shape (the existing goal_shape_for),
  failure_text, failure_class, context {tools, packages,
  capability, capability_version, deliverables},
  repair {description, tests_added, fixed_at} or null,
  repair_count, times_seen, first_seen, last_seen.

Rules, deterministic throughout:

- failure_class comes from keyword rules, not a model:
  timeout/rate-limit/network/API errors are transient,
  "unknown tool" is capability, "unverified criterion"
  is criterion, missing/content mismatches are verify,
  everything else is execution. The class is a coarse
  bucket for retrieval, never a decision, so a heuristic
  here is acceptable; the raw failure text is always kept.
- recurrence merges: a new failure with the same
  goal_shape and class as an existing lesson increments
  times_seen and refreshes last_seen instead of adding a
  duplicate. The first failure text is kept; the count
  answers "did it fail again later".
- retrieval ranks by shape-token Jaccard, the same
  measure the experience store uses, above a threshold
  (default 0.3, the DAG similarity default). Best shape
  first; each returned copy carries match_score.
- the store is capped (default 1000 lessons): past the
  cap, the stalest last_seen lesson is dropped. Lessons
  must not grow without bound.
- record_repair attaches the fix to a lesson by id. A
  repair without a lesson id is not guessed: the caller
  finds the lesson first (lessons_for) and names it.

Not wired into the loop yet, deliberately. Recording
on every failed run and recalling before every similar
build is the integration step, and it interacts with
the local-hub decision (where lessons surface at plan
time). This module is the store and the API; the wiring
is a separate decision.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .experience import goal_shape_for

LESSONS_FILENAME = "failure_lessons.json"
DEFAULT_THRESHOLD = 0.3
DEFAULT_MAX_LESSONS = 1000

_CLASS_RULES = (
    ("transient", ("timeout", "timed out", "rate limit", "rate_limit",
                   "network", "connection", "api error", "503", "502",
                   "429")),
    ("capability", ("unknown tool", "no such tool", "missing tool",
                    "not installed", "no module named")),
    ("criterion", ("unverified criterion",)),
    ("verify", ("does not contain", "not found", "missing",
                "does not exist", "failed verification")),
)


def classify_failure(failure_text: str) -> str:
    """Coarse deterministic bucket for a failure text."""
    text = (failure_text or "").lower()
    for failure_class, keywords in _CLASS_RULES:
        if any(k in text for k in keywords):
            return failure_class
    return "execution"


def _shape_overlap(a: str, b: str) -> float:
    set_a, set_b = set(a.split()), set(b.split())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FailureLessons:
    """Structured store of failure lessons."""

    def __init__(self, home, max_lessons: int = DEFAULT_MAX_LESSONS):
        self.home = Path(home)
        self.max_lessons = max_lessons

    def _path(self) -> Path:
        return self.home / LESSONS_FILENAME

    def _read(self) -> list:
        path = self._path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, lessons: list) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path().with_suffix(".tmp")
        tmp_path.write_text(json.dumps(lessons, indent=2, sort_keys=True))
        os.replace(tmp_path, self._path())

    def get(self, lesson_id: str):
        for lesson in self._read():
            if lesson.get("id") == lesson_id:
                return lesson
        return None

    def list(self) -> list:
        return self._read()

    def record_failure(self, goal: str, failure_text: str,
                       context: dict | None = None) -> dict:
        """Record a failure, merging recurrences of the same
        shape and class. Returns the lesson (new or merged)."""
        if not (goal or "").strip():
            raise ValueError("failure lesson needs a goal")
        failure_text = (failure_text or "")[:500]
        shape = goal_shape_for(goal)
        failure_class = classify_failure(failure_text)
        lessons = self._read()
        for lesson in lessons:
            if (lesson.get("goal_shape") == shape
                    and lesson.get("failure_class") == failure_class):
                lesson["times_seen"] = lesson.get("times_seen", 1) + 1
                lesson["last_seen"] = _now()
                self._write(lessons)
                return lesson
        lesson = {
            "id": uuid.uuid4().hex,
            "goal": goal,
            "goal_shape": shape,
            "failure_text": failure_text,
            "failure_class": failure_class,
            "context": dict(context or {}),
            "repair": None,
            "repair_count": 0,
            "times_seen": 1,
            "first_seen": _now(),
            "last_seen": _now(),
        }
        lessons.append(lesson)
        while len(lessons) > self.max_lessons:
            stalest = min(lessons, key=lambda e: e.get("last_seen", ""))
            lessons.remove(stalest)
        self._write(lessons)
        return lesson

    def record_repair(self, lesson_id: str, repair_description: str,
                      tests_added=None) -> dict:
        """Attach the fix to a lesson: what repaired it and
        which tests were added with the fix."""
        lessons = self._read()
        for lesson in lessons:
            if lesson.get("id") == lesson_id:
                lesson["repair"] = {
                    "description": repair_description,
                    "tests_added": list(tests_added or []),
                    "fixed_at": _now(),
                }
                lesson["repair_count"] = lesson.get("repair_count", 0) + 1
                lesson["last_seen"] = _now()
                self._write(lessons)
                return lesson
        raise ValueError(f"unknown lesson: {lesson_id}")

    def lessons_for(self, goal: str, limit: int = 3,
                    threshold: float = DEFAULT_THRESHOLD) -> list:
        """Best lessons for a goal, best shape match first.
        Each returned copy carries match_score."""
        shape = goal_shape_for(goal)
        scored = []
        for lesson in self._read():
            score = _shape_overlap(lesson.get("goal_shape", ""), shape)
            if score >= threshold:
                copy = dict(lesson)
                copy["match_score"] = round(score, 3)
                scored.append((score, lesson.get("times_seen", 1), copy))
        scored.sort(key=lambda kv: (kv[0], kv[1]), reverse=True)
        return [copy for _, _, copy in scored[:limit]]
