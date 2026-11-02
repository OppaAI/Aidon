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
as it gets a fresh budget — UNLESS the run site passes
a budget_key, in which case counts and seconds persist
in <home>/acquisition_budgets.json across that mission's
resumed segments and are cleared by the site when the
mission finishes. A budget_key names one mission (one
chat's one goal), never a whole chat.

What it counts (kinds):
  tool:       a tool/plugin build adopted in-run
              (adopt_tool_plugin).
  skill:      a skill promoted from a candidate in-run.
              The mission harvest at finalize tests each
              candidate and promotes on a passing report,
              bounded by max_skills; the learn cycle keeps
              its own daily cap.
  mcp:        a new MCP server connection (mcp_connect).
  dependency: a dependency set installed in-run
              (package installs, apt installs, and
              capability image builds).
  repair:     a capability repair cycle (a repair
              attempt after a failure).
  Time accounting, reconciled with the image builder:
  max_acquisition_seconds (default 600) is the per-run
  budget across ALL acquisitions; the builder's
  setup_timeout (default 1200) is the wall clock of a
  SINGLE image build. The time check runs between
  acquisitions, so a single build can run past the
  600s budget; the budget stops the NEXT acquisition,
  it does not interrupt the build in flight. The two
  numbers measure different things and do not
  conflict.

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
  An owner rejection is not a failed attempt: paths
  that approve first (MCP, installs) count only after
  approval passes; plugin adoption still counts first,
  a known ordering gap.

Exhaustion fails the run, like the run budget: the
raised AcquisitionBudgetExceeded names the exhausted
kind and the counts, so the final report explains
what stopped, instead of the run limping on with
repeated failed acquisitions.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


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
    home: Path | None = None
    budget_key: str | None = None
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

    def remaining_seconds(self) -> float:
        """Acquisition time left. A deadline holder clamps
        its timeouts to this, so a long operation cannot
        run past the budget unchecked."""
        return max(0.0, float(self.max_acquisition_seconds)
                   - float(self.acquisition_seconds))

    def _budget_path(self):
        if self.home is None or not self.budget_key:
            return None
        return Path(self.home) / "acquisition_budgets.json"

    def save(self) -> None:
        """Persist counts and seconds under budget_key.
        Best effort: a persistence failure must never fail
        the run it is accounting for."""
        path = self._budget_path()
        if path is None:
            return
        try:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
            if not isinstance(data, dict):
                data = {}
            data[str(self.budget_key)] = {
                "tools": int(self.tools),
                "skills": int(self.skills),
                "mcp": int(self.mcp),
                "dependency_sets": int(self.dependency_sets),
                "repair_cycles": int(self.repair_cycles),
                "acquisition_seconds": round(float(self.acquisition_seconds), 1),
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception:
            pass

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
        self.save()

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
            self.save()

    def summary(self) -> dict:
        return {
            "tools": self.tools,
            "skills": self.skills,
            "mcp": self.mcp,
            "dependency_sets": self.dependency_sets,
            "repair_cycles": self.repair_cycles,
            "acquisition_seconds": round(self.acquisition_seconds, 1),
        }

_BUDGET_COUNTS = ("tools", "skills", "mcp", "dependency_sets",
                  "repair_cycles")


def load_budget_record(home, key: str) -> dict | None:
    """Saved counts for one mission's budget_key, or None."""
    try:
        data = json.loads(
            (Path(home) / "acquisition_budgets.json").read_text(
                encoding="utf-8"))
    except Exception:
        return None
    record = data.get(str(key)) if isinstance(data, dict) else None
    return dict(record) if isinstance(record, dict) else None


def discard_budget_record(home, key: str) -> None:
    """Delete one mission's budget record (mission finished).
    Best effort; a missing record is not an error."""
    try:
        path = Path(home) / "acquisition_budgets.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or str(key) not in data:
            return
        del data[str(key)]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def budget_key_for(conversation_key: str, goal: str) -> str:
    """Stable mission key: one chat's one goal. Fresh runs
    and their proceeds share (conversation_key, goal), so
    they share the record; a new mission with different
    goal text gets a fresh record."""
    import hashlib
    digest = hashlib.sha1(
        f"{conversation_key or ''}\n{goal or ''}".encode()).hexdigest()[:16]
    return f"mission:{digest}"
