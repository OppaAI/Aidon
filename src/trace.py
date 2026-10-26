"""Trace events: what a run actually did, in order.

A run emits trace events, one per stage, with a
timestamp, a run id, a project id, a kind, a status,
and a detail string. Kinds, in run order:
interpret, research, recall, plan, sandbox, verify,
approval, live, learn. A trace event never contains
a secret, a token, a key, a password, or a file
content value, only names, counts, and statuses.
That is what makes a trace safe to show on screen
and in a demo video.

Trace events are written to <home>/trace/<run_id>.jsonl,
one JSON object per line, in order, and they are
also kept in memory on the recorder, so a test can
read them without reading a file. The factory
visualization, when it is built, renders these
events, it does not invent its own trace.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass
class TraceEvent:
    kind: str
    status: str
    detail: str = ""
    run_id: str = ""
    project_id: str = "default"
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat(timespec="seconds")

    def to_dict(self) -> dict:
        return {"kind": self.kind, "status": self.status,
                "detail": self.detail, "run_id": self.run_id,
                "project_id": self.project_id, "timestamp": self.timestamp}


class TraceRecorder:
    def __init__(self, home: Path, run_id: str | None = None,
                 project_id: str = "default"):
        self.home = Path(home)
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.project_id = project_id
        self.events: list[TraceEvent] = []
        self.trace_dir = self.home / "trace"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.trace_path = self.trace_dir / f"{self.run_id}.jsonl"

    def record(self, kind: str, status: str, detail: str = "") -> TraceEvent:
        event = TraceEvent(kind=kind, status=status, detail=detail,
                           run_id=self.run_id, project_id=self.project_id)
        self.events.append(event)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict()) + "\n")
        return event

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]
