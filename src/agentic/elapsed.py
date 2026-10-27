"""Elapsed time excluding approval wait, for honest run timing.

A run's wall-clock time includes time spent waiting for
an owner to approve a card, which is not work Aidon did.
Reporting wall-clock alone therefore overstates how long
a run took to compute, and makes a fast run look slow
when the owner was away. This timer separates the two:
total elapsed, approval wait, and active elapsed, which
is total minus approval wait, never below zero.

Class ElapsedTimer:
- __init__(time_fn=None): time_fn is an injectable clock
  returning a float, defaulting to time.monotonic. Tests
  pass a fake clock, production uses monotonic, which is
  not affected by system clock changes.
- start(): starts the timer and resets approval totals.
- note_approval_start() / note_approval_end(): mark the
  start and end of one approval wait. Nested starts are
  counted once, only the outermost interval counts, so
  overlapping waits do not double-count.
- approval_wait(): a context manager, use it as
  `with timer.approval_wait():` around an approval wait.
  It calls note_approval_start on entry and
  note_approval_end on exit, including on exception.
- approval_wait_seconds(): the total approval wait so
  far, including an interval still in progress.
- elapsed_total(): now minus start, or stop time minus
  start if stop() was called, or 0 if never started.
- elapsed_active(): elapsed_total minus approval wait,
  floored at 0.

Limits: this timer measures time, not tokens or steps.
It cannot tell why an approval waited, only how long,
and it does not pause the run, it only records.
"""

from __future__ import annotations

import time
from contextlib import contextmanager


class ElapsedTimer:
    def __init__(self, time_fn=None):
        self._time = time_fn or time.monotonic
        self._start: float | None = None
        self._end: float | None = None
        self._approval_total: float = 0.0
        self._approval_start: float | None = None
        self._approval_depth: int = 0

    def start(self) -> "ElapsedTimer":
        self._start = self._time()
        self._end = None
        self._approval_total = 0.0
        self._approval_start = None
        self._approval_depth = 0
        return self

    def stop(self) -> "ElapsedTimer":
        if self._start is not None and self._end is None:
            self._end = self._time()
        return self

    def note_approval_start(self) -> None:
        if self._approval_depth == 0:
            self._approval_start = self._time()
        self._approval_depth += 1

    def note_approval_end(self) -> None:
        if self._approval_depth <= 0:
            return
        self._approval_depth -= 1
        if self._approval_depth == 0 and self._approval_start is not None:
            self._approval_total += self._time() - self._approval_start
            self._approval_start = None

    @contextmanager
    def approval_wait(self):
        self.note_approval_start()
        try:
            yield self
        finally:
            self.note_approval_end()

    def approval_wait_seconds(self) -> float:
        total = self._approval_total
        if self._approval_depth > 0 and self._approval_start is not None:
            total += self._time() - self._approval_start
        return total

    def elapsed_total(self) -> float:
        if self._start is None:
            return 0.0
        end = self._end if self._end is not None else self._time()
        return max(0.0, end - self._start)

    def elapsed_active(self) -> float:
        return max(0.0, self.elapsed_total() - self.approval_wait_seconds())
