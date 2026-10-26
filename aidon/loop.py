"""Goal loop, skeleton version: research, plan, sandbox, verify,
approve, execute, learn — in that order, with evidence at each step.

The loop is deliberately small. It does not try to do everything
the full agent will do later. It proves the shape: a goal becomes
a plan, the plan is tested in the sandbox, the result is verified
against files that actually exist, a human approves before any
live action, and what was learned is recorded in a simple JSON
record under the Aidon home.

A result is verified by evidence, not by assertion. If the plan
says a file will exist, the verifier checks that the file exists
in the sandbox workdir and reads it. If it does not, the run
fails, it does not ask for approval, and it does not go live.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .approval import ApprovalGate, Decision
from .model_client import Message, ModelClient
from .sandbox import Sandbox


@dataclass
class Plan:
    steps: list[str] = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)


@dataclass
class RunReport:
    status: str
    goal: str
    plan: Plan
    verified: bool
    approved: bool
    files: list[str] = field(default_factory=list)
    error: str = ""


class GoalRunner:
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path):
        self.client = client
        self.sandbox = sandbox
        self.gate = gate
        self.home = Path(home)

    def _plan(self, goal: str) -> Plan:
        """Ask the model for a plan, fall back to a safe default.

        The model reply is a proposal, not a fact. The loop only
        trusts it after the sandbox run and the file check below.
        """
        reply = self.client.chat([
            Message("system", "Propose a short plan as JSON with keys "
                              "steps, deliverables, criteria."),
            Message("user", goal),
        ])
        try:
            data = json.loads(reply)
            return Plan(
                steps=[str(s) for s in data.get("steps", [])],
                deliverables=[str(d) for d in data.get("deliverables", [])],
                criteria=[str(c) for c in data.get("criteria", [])],
            )
        except (json.JSONDecodeError, AttributeError):
            return Plan(
                steps=["Write the deliverable in the sandbox workdir."],
                deliverables=["deliverable.md"],
                criteria=["deliverable.md exists and is non-empty"],
            )

    def run(self, goal: str) -> RunReport:
        plan = self._plan(goal)
        if not plan.deliverables:
            return RunReport("failed", goal, plan, False, False,
                             error="plan has no deliverables")
        # Sandbox: write each deliverable as a file, so the verifier
        # has something real to check. Later versions will run the
        # plan's real steps here; the skeleton proves the gate.
        for name in plan.deliverables:
            target = self.sandbox.workdir / name
            if target.parent != self.sandbox.workdir and \
                    self.sandbox.workdir not in target.parents:
                return RunReport("failed", goal, plan, False, False,
                                 error="deliverable outside workdir")
            result = self.sandbox.run(
                ["sh", "-c", f"printf 'Goal: {goal}\\n' > {name}"])
            if not result.ok:
                return RunReport("failed", goal, plan, False, False,
                                 error=f"sandbox failed: {result.stderr[:200]}")
        # Verify: every deliverable exists and is non-empty.
        files = []
        for name in plan.deliverables:
            path = self.sandbox.workdir / name
            if not path.exists() or path.stat().st_size == 0:
                return RunReport("failed", goal, plan, False, False,
                                 error=f"missing deliverable: {name}")
            files.append(name)
        # Approve: human decides before anything live. No approval
        # means no live step, and the run ends here, safely.
        decision: Decision = self.gate.ask(
            "live", "Approve live execution?",
            f"Goal: {goal}\nDeliverables: {', '.join(files)}")
        approved = decision.verdict == "approved"
        if not approved:
            return RunReport("rejected", goal, plan, True, False,
                             files=files)
        # Learn: record the run, in plain JSON, under the home.
        # Later versions will turn this into reusable skills and
        # workflows; the skeleton records the shape.
        learn_dir = self.home / "learned"
        learn_dir.mkdir(parents=True, exist_ok=True)
        record = {"goal": goal, "deliverables": files,
                  "criteria": plan.criteria}
        (learn_dir / "last_run.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        return RunReport("done", goal, plan, True, True, files=files)
