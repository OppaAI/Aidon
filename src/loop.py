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
                 gate: ApprovalGate, home: Path, memory=None,
                 promotion_ask=None):
        self.client = client
        self.sandbox = sandbox
        self.gate = gate
        self.home = Path(home)
        # promotion_ask: a chat question callback, point -> bool.
        # If set, and auto-promote is off, it is asked at the
        # end of a successful run, for that run's episodic
        # point. If unset, promotion is left to the caller,
        # via memory.promote_with_ask() or promote_to_ltm().
        self.promotion_ask = promotion_ask
        # Optional memory framework (see memory.py, experience.py).
        # When present, the loop recalls Experiences before
        # planning fresh, builds an Experience after a successful
        # run, and records episodic memory. When absent, the
        # skeleton loop runs unchanged, as in the earlier tests.
        self.memory = memory

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
        # Experience reuse: if a previous successful run left an
        # Experience with DAG nodes for a similar goal, start
        # from those nodes, adapted, instead of planning fresh.
        # The sandbox and verifier still check the result.
        if self.memory is not None:
            try:
                from .experience import recall_experiences

                experiences = recall_experiences(self.memory, goal,
                                                 limit=1)
                if experiences and experiences[0].dag_nodes:
                    nodes = experiences[0].dag_nodes
                    plan = Plan(
                        steps=[str(n.get("tool", "")) for n in nodes],
                        deliverables=[str(n["deliverable"])
                                      for n in nodes
                                      if n.get("deliverable")],
                        criteria=[])
                    if plan.deliverables:
                        return self._execute_plan(goal, plan)
            except Exception:
                pass
        plan = self._plan(goal)
        return self._execute_plan(goal, plan)

    def _execute_plan(self, goal: str, plan: Plan) -> RunReport:
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
        learn_dir = self.home / "learned"
        learn_dir.mkdir(parents=True, exist_ok=True)
        record = {"goal": goal, "deliverables": files,
                  "criteria": plan.criteria}
        (learn_dir / "last_run.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        if self.memory is not None:
            # Episodic memory, project-scoped, from this run.
            episodic = self.memory.record_episodic(
                "default", goal, {"deliverables": files})
            # Experience, procedural LTM, built after a successful
            # run only, so the next run can reuse this workflow.
            try:
                from .experience import build_experience, store_experience

                nodes = [{"tool": "write", "args": {"file": f},
                          "deliverable": f} for f in files]
                experience = build_experience(
                    goal, goal, nodes, ["write"], [], "done",
                    "Reused workflow from a successful run.")
                store_experience(self.memory, experience)
            except Exception:
                pass
            # Promotion to LTM: auto if the env flag says so,
            # otherwise ask via promotion_ask if one was given,
            # otherwise leave it to the caller. A "no" leaves
            # episodic memory project-scoped only.
            try:
                if self.promotion_ask is not None:
                    self.memory.promote_with_ask(
                        episodic, self.promotion_ask, kind="semantic")
                elif self.memory.auto_promote_enabled():
                    self.memory.promote_to_ltm(episodic, kind="semantic")
            except Exception:
                pass
        return RunReport("done", goal, plan, True, True, files=files)
