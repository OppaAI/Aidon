"""Capability acquisition governor: one per-run budget for
how much a mission may grow Aidon's toolbox.

The run budget (tokens, steps, time) bounds execution,
but it does not bound acquisition: fifty steps can in
principle adopt dozens of tools, connect several MCP
servers, and build several images. This governor closes
that gap with a single object that counts every
acquisition a run attempts, across all kinds, and stops
the run with a clear message when any cap is hit.

Defaults are the starting policy, not universal limits:
they are configurable per key (AIDON_ACQ_MAX_*), and a
mission with explicit owner authorization runs with a
larger budget. Counts are per run, the same unit as the
run budget: a resumed run gets a fresh governor, just
as it gets a fresh budget.

What it counts (kinds):
  tool:       a tool/plugin build adopted in-run
              (adopt_tool_plugin).
  skill:      a skill promoted from experience in-run.
              Reserved: no run path promotes skills today
              (promotion runs through the learn cycle,
              which has its own daily cap), so this
              counter is for the wiring when one lands.
  mcp:        a new MCP server connection (mcp_connect).
  dependency: a dependency set installed in-run
              (package installs, apt installs, and
              capability image builds).
  repair:     a capability repair cycle (a repair
              attempt after a failure).

What it does NOT do, deliberately:
- Reuse-before-creation is the loop's recall and
  triage, which run before any acquisition; the
  governor counts what remains, it does not duplicate
  that search.
- Duplicate detection is name-based at the stores
  (registry and skill library reject collisions).
  Same-purpose capabilities under different names are
  not detected; that needs semantic comparison.
- Trust promotion is never the governor's to give:
  validated never becomes trusted without owner
  approval, enforced by the registry and the skill
  library, not by counts.
- A failed acquisition still consumes its count.
  The budget is about attempts: without that, a
  failing capability could be retried until the run
  budget, not the acquisition budget, stops it.

Exhaustion fails the run, like the run budget: the
raised AcquisitionBudgetExceeded names the exhausted
kind and the counts, so the final report explains
what stopped, instead of the run limping on with
repeated failed acquisitions.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


class AcquisitionBudgetExceeded(Exception):
    """A per-run acquisition cap was hit. Fails the run."""


_KIND_ATTRS = {
    "tool": "tools",
    "skill": "skills",
    "mcp": "mcp",
    "dependency": "dependency_sets",
    "repair": "repair_cycles",
}

_KIND_CAPS = {
    "tool": "max_tools",
    "skill": "max_skills",
    "mcp": "max_mcp",
    "dependency": "max_dependency_sets",
    "repair": "max_repair_cycles",
}


@dataclass
class AcquisitionGovernor:
    max_tools: int = 2
    max_skills: int = 2
    max_mcp: int = 1
    max_dependency_sets: int = 2
    max_repair_cycles: int = 3
    max_acquisition_seconds: float = 600.0
    tools: int = 0
    skills: int = 0
    mcp: int = 0
    dependency_sets: int = 0
    repair_cycles: int = 0
    acquisition_seconds: float = 0.0

    def _check_time(self) -> None:
        if self.acquisition_seconds >= self.max_acquisition_seconds:
            raise AcquisitionBudgetExceeded(
                "acquisition time budget exhausted: "
                f"{self.acquisition_seconds:.0f}s used of "
                f"{self.max_acquisition_seconds:.0f}s allowed; "
                "counts: " + self._counts_str())

    def _counts_str(self) -> str:
        return (f"tools={self.tools}/{self.max_tools}, "
                f"skills={self.skills}/{self.max_skills}, "
                f"mcp={self.mcp}/{self.max_mcp}, "
                f"dependency_sets={self.dependency_sets}/"
                f"{self.max_dependency_sets}, "
                f"repair_cycles={self.repair_cycles}/"
                f"{self.max_repair_cycles}")

    def acquire(self, kind: str) -> None:
        """Count one acquisition attempt of kind, or raise.
        Call BEFORE the acquisition runs: a failed attempt
        still consumes budget, so retries cannot loop."""
        if kind not in _KIND_ATTRS:
            raise ValueError(f"unknown acquisition kind: {kind!r}")
        self._check_time()
        attr, cap_attr = _KIND_ATTRS[kind], _KIND_CAPS[kind]
        if getattr(self, attr) >= getattr(self, cap_attr):
            raise AcquisitionBudgetExceeded(
                f"acquisition budget exhausted for {kind}: "
                f"{getattr(self, attr)} of {getattr(self, cap_attr)} "
                f"allowed; counts: {self._counts_str()}")
        setattr(self, attr, getattr(self, attr) + 1)

    @contextmanager
    def timed(self, kind: str):
        """acquire(kind) plus elapsed-time accounting for
        the acquisition itself. Time spent inside counts
        toward the shared acquisition time budget."""
        self.acquire(kind)
        start = time.monotonic()
        try:
            yield
        finally:
            self.acquisition_seconds += time.monotonic() - start

    def summary(self) -> dict:
        return {
            "tools": self.tools,
            "skills": self.skills,
            "mcp": self.mcp,
            "dependency_sets": self.dependency_sets,
            "repair_cycles": self.repair_cycles,
            "acquisition_seconds": round(self.acquisition_seconds, 1),
        }
