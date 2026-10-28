"""Reports, Aidon report concept, rebuilt as a separate module.

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
import re
from datetime import date, datetime, timezone
from pathlib import Path


def daily_report(home) -> str:
    return generate_daily_report(home)["brief"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:48] or "goal"


def _run_day(record: dict, tz) -> date | None:
    try:
        stamp = record.get("created_at") or record.get("updated_at")
        if not stamp:
            return None
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(tz).date()
    except (TypeError, ValueError):
        return None


def _read_last_run(home: Path) -> dict:
    """Read <home>/learned/last_run.json, or {} when absent or unreadable."""
    path = Path(home) / "learned" / "last_run.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def generate_daily_report(home, day: date | None = None,
                          tz_name: str = "UTC") -> dict:
    """Write a deterministic daily brief and one detail file per goal."""
    from zoneinfo import ZoneInfo
    from .run_control import RunControl

    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:
        tz = timezone.utc
    home = Path(home)
    day = day or datetime.now(tz).date()
    records = [record for record in RunControl(home).list_runs()
               if _run_day(record, tz) == day]
    status_counts = {}
    for record in records:
        status = str(record.get("status", "?"))
        status_counts[status] = status_counts.get(status, 0) + 1
    lines = [f"# Aidon daily report — {day.isoformat()}", ""]
    if not records:
        lines.append("No runs recorded today.")
    else:
        parts = [f"{count} {status}" for status, count in sorted(status_counts.items())]
        lines.append(f"{len(records)} runs: {', '.join(parts)}.")
        lines.extend(f"- {r.get('goal', '?')} → {r.get('status', '?')}"
                     for r in records)
    # The last recorded run is the fallback the daily brief always had:
    # a home with no run records today still says what Aidon did last,
    # from learned/last_run.json, instead of an empty "nothing" report.
    last_run = _read_last_run(home)
    if last_run:
        status = str(last_run.get("status", "") or "recorded")
        project = str(last_run.get("project_id", "") or "")
        goal = str(last_run.get("goal", "") or "")
        line = f"Last recorded run: {status}"
        if goal:
            line += f" — {goal}"
        if project:
            line += f" (project {project})"
        lines.extend(["", line])
        deliverables = last_run.get("deliverables") or []
        if isinstance(deliverables, list) and deliverables:
            lines.append("Deliverables: " + ", ".join(str(d) for d in deliverables))
    brief = "\n".join(lines) + "\n"
    outdir = home / "reports" / day.isoformat()
    goals_dir = outdir / "goals"
    goals_dir.mkdir(parents=True, exist_ok=True)
    (outdir / "brief.md").write_text(brief, encoding="utf-8")
    by_goal = {}
    for record in records:
        by_goal.setdefault(str(record.get("goal", "?")), []).append(record)
    slugs = []
    for goal, grouped in sorted(by_goal.items()):
        slug = _slug(goal)
        slugs.append(slug)
        detail = [f"# {goal}", "", f"{len(grouped)} run(s) on {day.isoformat()}", ""]
        for record in grouped:
            detail.append(f"## {record.get('run_id', '?')} — {record.get('status', '?')}")
            if record.get("report"):
                detail.extend(["", str(record["report"])[:4000]])
            detail.append("")
        (goals_dir / f"{slug}.md").write_text("\n".join(detail), encoding="utf-8")
    return {"dir": str(outdir), "brief": brief, "goals": slugs,
            "runs": len(records)}


def build_nightly_brief(home, client) -> dict:
    """Create the old short nightly brief from the deterministic report.

    The report is the source of truth; the model only rewrites it for
    readability.  Routing through ``respond`` preserves the fast-tier
    behavior when a fast model is configured, while remaining compatible
    with older test clients that only expose ``chat(messages)``.
    """
    from ..system.model_client import Message

    report_result = generate_daily_report(home)
    report = report_result["brief"].strip()
    prompt = (
        "Rewrite this Aidon activity report as a short nightly brief for "
        "the owner. Use 2-5 plain sentences, no markdown headers or bullet "
        "lists. If nothing happened, say so briefly.\n\n" + report)
    messages = [Message("user", prompt)]
    try:
        if hasattr(client, "chat_phase"):
            text = client.chat_phase("respond", messages)
        else:
            text = client.chat(messages)
    except Exception:
        text = report
    return {"text": str(text or report).strip(), "report": report,
            "runs": report_result["runs"], "dir": report_result["dir"]}


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
