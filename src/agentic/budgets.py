"""Budgets: token, step, and time limits, enforced, not advisory.

Limits, defaults from the old design, kept here:
- 200,000 tokens, estimated, per run.
- 50 steps, per run.
- 3,600 seconds, 60 minutes, per run.
A budget is set by the owner, in config, per run,
never by the model. When a budget is exceeded, the
run fails, it does not continue, it does not ask for
approval, and it does not go live.

Token counting, honest limit:
Token counts here are estimates, about one token per
four characters of model input and output, unless a
model client returns a real usage count, in which
case that count replaces the estimate. An estimate
is labeled as an estimate in the run record, never
presented as an exact model token count.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


class BudgetExceeded(Exception):
    pass


@dataclass
class Budget:
    max_tokens: int = 200000
    max_steps: int = 50
    max_seconds: int = 3600
    tokens_used: int = 0
    steps_used: int = 0
    started_at: float = 0.0

    def __post_init__(self) -> None:
        # Swarm sub-runs share one Budget across threads, so
        # every counter mutation and read goes through this
        # lock. An RLock because check() runs inside the add
        # methods. Not a dataclass field: a lock is runtime
        # state, not budget data, and must not leak into
        # equality, repr, or serialization.
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            self.started_at = time.time()

    def estimate_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def add_tokens(self, text: str, exact: int | None = None) -> None:
        with self._lock:
            self.tokens_used += exact if exact is not None else self.estimate_tokens(text)
            self.check()

    def add_step(self) -> None:
        with self._lock:
            self.steps_used += 1
            self.check()

    def elapsed_seconds(self) -> float:
        with self._lock:
            if not self.started_at:
                return 0.0
            return time.time() - self.started_at

    def check(self) -> None:
        with self._lock:
            if self.tokens_used > self.max_tokens:
                raise BudgetExceeded(f"token budget exceeded: {self.tokens_used} > {self.max_tokens}")
            if self.steps_used > self.max_steps:
                raise BudgetExceeded(f"step budget exceeded: {self.steps_used} > {self.max_steps}")
            if self.started_at and self.elapsed_seconds() > self.max_seconds:
                raise BudgetExceeded(f"time budget exceeded: {self.elapsed_seconds():.0f}s > {self.max_seconds}s")

    def to_dict(self) -> dict:
        with self._lock:
            return {"max_tokens": self.max_tokens, "tokens_used": self.tokens_used,
                    "max_steps": self.max_steps, "steps_used": self.steps_used,
                    "max_seconds": self.max_seconds,
                    "elapsed_seconds": round(self.elapsed_seconds(), 1)}
