"""Full home export as Markdown files.

Renders the registry as Markdown files under an output
directory, one file per mission, skill, DAG, and run,
plus an index table per section. Read-only with respect
to the Aidon home: the home is opened, never written.
A missing or unreadable store section writes zero rows
for that section, it never crashes the export.

Sources, all existing new-repo stores:
  missions — <home>/missions/<id>/mission.json (+ snapshot)
  skills   — <home>/skill_lifecycle.json
  dags     — <home>/dags/<id>.json
  runs     — <home>/runs/<id>.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def _safe(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "unnamed"))
    return safe or "unnamed"


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _collect_missions(home: Path) -> list[dict]:
    missions = []
    missions_dir = home / "missions"
    if not missions_dir.is_dir():
        return missions
    for directory in sorted(missions_dir.iterdir()):
        if not directory.is_dir():
            continue
        mission = _read_json(directory / "mission.json")
        if mission is None:
            continue
        snapshot = _read_json(directory / "snapshot.json") or {}
        spec = mission.get("spec") or {}
        if not isinstance(spec, dict):
            spec = {}
        criteria = spec.get("criteria") or mission.get("deliverables") or []
        if isinstance(criteria, str):
            criteria = [criteria]
        missions.append({
            "id": str(mission.get("conversation_id") or mission.get("project_id") or directory.name),
            "goal": str(spec.get("goal") or mission.get("goal") or ""),
            "state": str(mission.get("status") or snapshot.get("status")
                         or ""),
            "history": mission.get("history") or [],
            "criteria": criteria,
            "report": str(snapshot.get("report") or mission.get("report")
                          or ""),
        })
    return missions


def _collect_skills(home: Path) -> list[dict]:
    data = _read_json(home / "skill_lifecycle.json")
    if data is None:
        return []
    skills = []
    for name, entry in sorted(data.items()):
        if not isinstance(entry, dict):
            continue
        skills.append({
            "name": str(name),
            "state": str(entry.get("state") or ""),
            "version": entry.get("version", 1),
            "privileged": entry.get("privileged", False),
            "capabilities": entry.get("capabilities") or [],
            "successes": entry.get("successes", 0),
            "failures": entry.get("failures", 0),
            "versions": entry.get("versions") or [],
        })
    return skills


def _collect_dags(home: Path) -> list[dict]:
    dags = []
    dags_dir = home / "dags"
    if not dags_dir.is_dir():
        return dags
    for path in sorted(dags_dir.glob("*.json")):
        data = _read_json(path)
        if data is None:
            continue
        nodes = data.get("nodes") or []
        dags.append({"id": str(data.get("id") or path.stem),
                     "goal": str(data.get("goal") or ""),
                     "nodes": nodes})
    return dags


def _collect_runs(home: Path) -> list[dict]:
    runs = []
    runs_dir = home / "runs"
    if not runs_dir.is_dir():
        return runs
    for path in sorted(runs_dir.glob("*.json")):
        data = _read_json(path)
        if data is None:
            continue
        runs.append({
            "id": str(data.get("run_id") or path.stem),
            "goal": str(data.get("goal") or ""),
            "state": str(data.get("status") or ""),
            "conversation_id": str(data.get("conversation_id") or data.get("project_id") or ""),
            "cancelled": bool(data.get("cancelled", False)),
            "steer_notes": data.get("steer_notes") or [],
        })
    return runs


def _write_missions(outdir: Path, missions: list[dict]) -> None:
    section = outdir / "missions"
    section.mkdir(parents=True, exist_ok=True)
    rows = []
    for mission in missions:
        rows.append([mission["id"], mission["goal"][:80], mission["state"]])
        lines = [f"# Mission {mission['id']}", "",
                 f"**state:** {mission['state']}  ", "",
                 "## Goal", "", mission["goal"] or "(none)", ""]
        if mission["criteria"]:
            lines += ["## Acceptance criteria", ""]
            for criterion in mission["criteria"]:
                if isinstance(criterion, dict):
                    name = criterion.get("name", "")
                    status = criterion.get("status", "")
                    evidence = criterion.get("evidence", "")
                    lines.append(f"- **{name}** ({status}) — {evidence}")
                else:
                    lines.append(f"- {criterion}")
            lines.append("")
        if mission["history"]:
            lines += ["## History", ""]
            for item in mission["history"]:
                if isinstance(item, dict):
                    lines.append(
                        f"- `{str(item.get('at', ''))[:16]}` "
                        f"{item.get('from', '')} → "
                        f"**{item.get('to', '')}** — "
                        f"{item.get('evidence', '')}")
                else:
                    lines.append(f"- {item}")
            lines.append("")
        if mission["report"]:
            lines += ["## Report", "", mission["report"], ""]
        (section / f"{_safe(mission['id'])}.md").write_text(
            "\n".join(lines), encoding="utf-8")
    (section / "index.md").write_text(
        "# Missions\n\n" + _md_table(["id", "goal", "state"], rows),
        encoding="utf-8")


def _write_skills(outdir: Path, skills: list[dict]) -> None:
    section = outdir / "skills"
    section.mkdir(parents=True, exist_ok=True)
    rows = []
    for skill in skills:
        rows.append([skill["name"], f"v{skill['version']}",
                     skill["state"],
                     "yes" if skill["privileged"] else "no"])
        lines = [f"# Skill: {skill['name']}", "",
                 f"**state:** {skill['state']} · "
                 f"**version:** v{skill['version']} · "
                 f"**privileged:** {'yes' if skill['privileged'] else 'no'}",
                 "", f"successes: {skill['successes']} · "
                 f"failures: {skill['failures']}", ""]
        if skill["capabilities"]:
            lines += ["## Capabilities", ""]
            lines += [f"- {cap}" for cap in skill["capabilities"]]
            lines.append("")
        if skill["versions"]:
            lines += ["## Versions", ""]
            for version in skill["versions"]:
                lines.append(f"- {version}")
            lines.append("")
        (section / f"{_safe(skill['name'])}.md").write_text(
            "\n".join(lines), encoding="utf-8")
    (section / "index.md").write_text(
        "# Skills\n\n" + _md_table(
            ["name", "version", "state", "privileged"], rows),
        encoding="utf-8")


def _write_dags(outdir: Path, dags: list[dict]) -> None:
    section = outdir / "dags"
    section.mkdir(parents=True, exist_ok=True)
    rows = []
    for dag in dags:
        rows.append([dag["id"], dag["goal"][:80], str(len(dag["nodes"]))])
        lines = [f"# DAG: {dag['id']}", "",
                 "## Goal", "", dag["goal"] or "(none)", "",
                 "## Nodes", ""]
        for node in dag["nodes"]:
            if isinstance(node, dict):
                lines.append(
                    f"- {node.get('id', '')}: {node.get('tool', '')} — "
                    f"{node.get('deliverable', '')}")
            else:
                lines.append(f"- {node}")
        lines.append("")
        (section / f"{_safe(dag['id'])}.md").write_text(
            "\n".join(lines), encoding="utf-8")
    (section / "index.md").write_text(
        "# DAGs\n\n" + _md_table(["id", "goal", "nodes"], rows),
        encoding="utf-8")


def _write_runs(outdir: Path, runs: list[dict]) -> None:
    section = outdir / "runs"
    section.mkdir(parents=True, exist_ok=True)
    rows = []
    for run in runs:
        rows.append([run["id"], run["goal"][:80], run["state"]])
        lines = [f"# Run {run['id']}", "",
                 f"**state:** {run['state']}  ",
                 f"**chat:** {run['conversation_id']}  ",
                 f"**cancelled:** {'yes' if run['cancelled'] else 'no'}",
                 "", "## Goal", "", run["goal"] or "(none)", ""]
        if run["steer_notes"]:
            lines += ["## Steer notes", ""]
            lines += [f"- {note}" for note in run["steer_notes"]]
            lines.append("")
        (section / f"{_safe(run['id'])}.md").write_text(
            "\n".join(lines), encoding="utf-8")
    (section / "index.md").write_text(
        "# Runs\n\n" + _md_table(["id", "goal", "state"], rows),
        encoding="utf-8")


def export_markdown(home, outdir) -> dict:
    """Render the home as Markdown files under `outdir`.

    Returns a summary dict: {missions, skills, dags, runs, dir}.
    Read-only with respect to `home`; only `outdir` is written.
    """
    home, outdir = Path(home), Path(outdir)
    try:
        missions = _collect_missions(home)
    except Exception:
        missions = []
    try:
        skills = _collect_skills(home)
    except Exception:
        skills = []
    try:
        dags = _collect_dags(home)
    except Exception:
        dags = []
    try:
        runs = _collect_runs(home)
    except Exception:
        runs = []
    outdir.mkdir(parents=True, exist_ok=True)
    _write_missions(outdir, missions)
    _write_skills(outdir, skills)
    _write_dags(outdir, dags)
    _write_runs(outdir, runs)
    (outdir / "index.md").write_text(
        "# Aidon export\n\n" + _md_table(
            ["section", "count"],
            [["missions", str(len(missions))],
             ["skills", str(len(skills))],
             ["dags", str(len(dags))],
             ["runs", str(len(runs))]]),
        encoding="utf-8")
    return {"missions": len(missions), "skills": len(skills),
            "dags": len(dags), "runs": len(runs), "dir": str(outdir)}
