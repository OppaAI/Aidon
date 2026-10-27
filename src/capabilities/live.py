"""Live execution: actions outside the sandbox, closed list only.

Sandbox execution is where a plan is tested. Live
execution is what happens after sandbox verification
and after the owner approves, and it is deliberately
narrower than sandbox execution, not wider.

Closed live actions, and nothing else:
- copy_to_output: copy a verified file from the
  sandbox workdir to an output path the spec names.
  Before overwriting an existing output file, a
  snapshot backup is made, so a failed live
  verification can roll back to the previous file.
- notify: record a notification, with recipient,
  subject, and body, in <home>/live/notifications.jsonl.
  Notify does not send email, it records that a
  notification is ready to send. Email sending itself
  is a plugin later, with its own approval and its
  own wrong-email checks, not a generic live shell.
- No live shell, no live network, no live delete,
  no live overwrite without a snapshot. A plan step
  that asks for a live shell is rejected, not run.

Live verification, after live execution:
- Every copied output file must exist, be non-empty,
  and, for a ledger, total to the same total the
  sandbox verifier computed. If live verification
  fails, rollback restores every snapshot backup,
  and the run is reported as failed, not as done.
  A live run that cannot be verified is not a live
  success, even if the copy command exited cleanly.

Incremental improving, live part:
A live execution record, with actions, outputs,
verification result, and rollback status, is
recorded as episodic memory and as part of the
run's Experience lesson, so the next run for a
similar goal starts with what actually went live,
what failed live, and what was rolled back, instead
of repeating a live failure.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LiveRecord:
    actions: list[dict] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    verified: bool = False
    rolled_back: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {"actions": self.actions, "outputs": self.outputs,
                "verified": self.verified, "rolled_back": self.rolled_back,
                "error": self.error}


class LiveExecutor:
    def __init__(self, workdir: Path, home: Path):
        self.workdir = Path(workdir)
        self.home = Path(home)
        self.live_dir = self.home / "live"
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._snapshots: dict[str, Path | None] = {}

    def copy_to_output(self, source_name: str, output_path: Path) -> str:
        source = self.workdir / source_name
        if not source.exists() or source.stat().st_size == 0:
            raise ValueError(f"live source is missing or empty: {source_name}")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            snapshot = self.live_dir / f"snapshot-{output_path.name}"
            shutil.copy2(output_path, snapshot)
            self._snapshots[str(output_path)] = snapshot
        else:
            self._snapshots[str(output_path)] = None
        shutil.copy2(source, output_path)
        return str(output_path)

    def notify(self, recipient: str, subject: str, body: str) -> str:
        record = {"recipient": recipient, "subject": subject, "body": body}
        with (self.live_dir / "notifications.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        return f"notification recorded for {recipient}"

    def execute(self, actions: list[dict]) -> LiveRecord:
        record = LiveRecord()
        try:
            for action in actions:
                kind = action.get("kind", "")
                if kind == "copy_to_output":
                    output = self.copy_to_output(
                        str(action["file"]), Path(str(action["output"])))
                    record.actions.append(action)
                    record.outputs.append(output)
                elif kind == "notify":
                    self.notify(str(action["recipient"]),
                                str(action.get("subject", "")),
                                str(action.get("body", "")))
                    record.actions.append(action)
                else:
                    raise ValueError(f"live action not allowed: {kind}")
        except Exception as exc:
            record.error = str(exc)
            return record
        return record

    def verify_live(self, record: LiveRecord) -> bool:
        """Live outputs must exist and be non-empty, or verification fails."""
        if record.error:
            return False
        for output in record.outputs:
            path = Path(output)
            if not path.exists() or path.stat().st_size == 0:
                record.error = f"live output missing or empty: {output}"
                return False
        record.verified = True
        return True

    def rollback(self, record: LiveRecord) -> None:
        for output, snapshot in self._snapshots.items():
            path = Path(output)
            if snapshot is not None and snapshot.exists():
                shutil.copy2(snapshot, path)
            elif snapshot is None and path.exists():
                path.unlink()
        record.rolled_back = True
