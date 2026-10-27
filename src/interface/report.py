"""Reports, old Aidon report concept, rebuilt as a separate module.

daily_report is the scheduler's daily report, re-exported here so the CLI report command
and the WebUI reports tab use the same function, not two copies. autonomy_report is new
here: it counts runs, traces, and learned last runs, and reports autonomy level from
counts, not from a claim: a run with an approval gate is not called fully autonomous,
a run with no gate in the record is not invented either. Per-tier router calls and
tokens come from the stored autonomy metrics file when it exists, else zero — never
from a live client, which a report function does not have.
"""

from __future__ import annotations

import json
from pathlib import Path


def daily_report(home) -> str:
    from .scheduler_loop import daily_report as _daily
    return _daily(home)


def autonomy_report(home) -> dict:
    home = Path(home)
    last_run_path = home / "learned" / "last_run.json"
    last_run = {}
    if last_run_path.is_file():
        try:
            last_run = json.loads(last_run_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            last_run = {}
    runs_dir = home / "runs"
    run_count = len(list(runs_dir.glob("*.json"))) if runs_dir.is_dir() else 0
    trace_dir = home / "trace"
    trace_count = len(list(trace_dir.glob("*"))) if trace_dir.is_dir() else 0
    # Per-tier router calls + tokens, from the stored metrics file
    # only (the report has no live client to ask). No file, zeros.
    router = {"router_fast_calls": 0, "router_strong_calls": 0,
              "router_fast_tokens": 0, "router_strong_tokens": 0}
    if (home / "autonomy_metrics.json").is_file():
        try:
            from ..agentic.autonomy_metrics import AutonomyMetrics
            summary = AutonomyMetrics(home).summary()
            for key in router:
                router[key] = int(summary.get(key, 0))
        except Exception:
            pass
    return {
        "last_run_status": last_run.get("status", "none"),
        "last_run_project": last_run.get("project_id", ""),
        "run_count": run_count,
        "trace_count": trace_count,
        "autonomy": "supervised, approval gate on live actions" if last_run else "no runs recorded",
        **router,
    }
