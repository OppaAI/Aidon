"""Goal loop: plan, execute in sandbox, verify, approve, learn.

Order: a goal becomes a plan, the plan's steps are executed
with sandbox tools, the result is verified by code against
files that actually exist, a human approves before any live
action, and what was learned is recorded.

Plan shape, from the model, as JSON:
  steps: [{tool, args, deliverable, criteria?}, ...]
  deliverables: [file, ...]  (also derived from steps)
  criteria: ["file exists", "file contains X", ...]
Old string steps are still accepted: a string step becomes
a write step for deliverable.md, so earlier tests keep
working. The model reply is a proposal, not a fact: the
loop only trusts it after tool execution and verification.

Repair, not re-plan, after a failure:
If a step fails or verification fails, the loop asks the
model for a repaired plan, with the failure list and the
list of passed deliverables. Passed deliverables are
untouched: repair steps may only fix failed steps and
failed criteria, they may not rewrite a passed file.
At most 3 attempts in total (1 initial + 2 repairs),
then the run fails, it does not ask for approval, and
it does not go live.

Memory, blank by design: new Aidon starts with empty
collections, no import from previous projects. When a
memory is present, the loop recalls Experiences before
planning fresh, records episodic memory scoped by
project_id, and builds an Experience after a successful
run only. Promotion is auto if the flag says so,
otherwise via promotion_ask, otherwise left to the caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .approval import ApprovalGate, Decision
from .model_client import Message, ModelClient
from .sandbox import Sandbox
from .tools import ToolRegistry
from .verify import verify_plan

MAX_ATTEMPTS = 3


@dataclass
class Step:
    tool: str = "write"
    args: dict = field(default_factory=dict)
    deliverable: str = ""
    criteria: str = ""


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)

    def normalized_steps(self) -> list[Step]:
        out = []
        for step in self.steps:
            if isinstance(step, Step):
                out.append(step)
            elif isinstance(step, dict):
                out.append(Step(
                    tool=str(step.get("tool", "write")),
                    args=dict(step.get("args", {})),
                    deliverable=str(step.get("deliverable", "")),
                    criteria=str(step.get("criteria", ""))))
            else:
                out.append(Step(tool="write", args={"file": "deliverable.md",
                                                    "text": str(step)},
                                deliverable="deliverable.md"))
        return out


@dataclass
class RunReport:
    status: str
    goal: str
    plan: Plan
    verified: bool
    approved: bool
    files: list[str] = field(default_factory=list)
    error: str = ""
    attempts: int = 1


class GoalRunner:
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path, memory=None,
                 promotion_ask=None, project_id: str = "default"):
        self.client = client
        self.sandbox = sandbox
        self.gate = gate
        self.home = Path(home)
        self.memory = memory
        self.promotion_ask = promotion_ask
        self.project_id = project_id
        self.tools = ToolRegistry(sandbox.workdir, sandbox=sandbox)

    def _plan(self, goal: str, failure_context: str = "",
              spec=None) -> Plan:
        system = ("Propose a short plan as JSON with keys steps, "
                  "deliverables, criteria. Each step is an object "
                  "with tool, args, deliverable. Tools: write, read, "
                  "list, shell, calculate. Keep every file inside "
                  "the workdir.")
        user = goal
        if spec is not None:
            spec_dict = spec.to_dict() if hasattr(spec, "to_dict") else spec
            user = (f"Goal: {goal}\n\nFrozen spec, binding for this plan, "
                    f"do not invent deliverables or constraints outside it:\n"
                    f"{json.dumps(spec_dict, indent=2)}")
        if failure_context:
            user = user + (f"\n\nPrevious attempt failed, repair only the "
                           f"failed parts, do not rewrite passed files:\n{failure_context}")
        reply = self.client.chat([Message("system", system),
                                  Message("user", user)])
        try:
            data = json.loads(reply)
            steps = data.get("steps", [])
            deliverables = [str(d) for d in data.get("deliverables", [])]
            if not deliverables:
                deliverables = [str(s.get("deliverable", ""))
                                for s in steps if isinstance(s, dict)
                                and s.get("deliverable")]
            return Plan(steps=steps, deliverables=deliverables,
                        criteria=[str(c) for c in data.get("criteria", [])])
        except (json.JSONDecodeError, AttributeError):
            return Plan(
                steps=[Step(tool="write", args={"file": "deliverable.md",
                                                "text": f"Goal: {goal}\n"},
                            deliverable="deliverable.md")],
                deliverables=["deliverable.md"],
                criteria=["deliverable.md exists"])

    def run(self, goal: str, spec=None) -> RunReport:
        # A frozen spec binds the planner: if one is given,
        # plan from it, do not plan fresh from the goal alone.
        # Experience reuse still applies, but only if its
        # deliverables match the spec's deliverables.
        self._current_spec = spec
        if spec is not None:
            spec_deliverables = spec.deliverables if hasattr(spec, "deliverables") else spec.get("deliverables", [])
            plan = self._plan(goal, spec=spec)
            if not plan.deliverables and spec_deliverables:
                plan.deliverables = list(spec_deliverables)
            return self._execute_plan(goal, plan)
        if self.memory is not None:
            try:
                from .experience import recall_experiences
                experiences = recall_experiences(self.memory, goal, limit=1)
                if experiences and experiences[0].dag_nodes:
                    nodes = experiences[0].dag_nodes
                    plan = Plan(
                        steps=nodes,
                        deliverables=[str(n["deliverable"])
                                      for n in nodes if n.get("deliverable")],
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
        passed: set[str] = set()
        current_plan = plan
        for attempt in range(1, MAX_ATTEMPTS + 1):
            failures: list[str] = []
            # Execute steps, skipping steps whose deliverable
            # already passed in an earlier attempt (repair rule).
            for step in current_plan.normalized_steps():
                if step.deliverable and step.deliverable in passed:
                    continue
                try:
                    if step.tool == "write" and not step.args:
                        step.args = {"file": step.deliverable,
                                     "text": f"Goal: {goal}\n"}
                    self.tools.call(step.tool, step.args)
                except Exception as exc:
                    failures.append(f"step {step.tool} failed: {exc}")
            # Verify: deliverables + plan criteria + step criteria.
            criteria = list(current_plan.criteria)
            for step in current_plan.normalized_steps():
                if step.criteria and step.criteria not in criteria:
                    criteria.append(step.criteria)
            ok, verify_failures, verified = verify_plan(
                self.sandbox.workdir, current_plan.deliverables, criteria)
            failures.extend(verify_failures)
            # Passed means existence AND no failure mentions
            # it. A ledger file that exists but totals wrong
            # is not passed, so repair can rewrite it, while
            # a good file that passed every check is untouched.
            for name in verified:
                if not any(name in msg for msg in failures):
                    passed.add(name)
            if ok:
                break
            if attempt == MAX_ATTEMPTS:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="; ".join(failures)[:500],
                                 attempts=attempt)
            # Repair: ask for a repaired plan, passed files untouched.
            context = ("Passed deliverables (do not rewrite): "
                       + ", ".join(sorted(passed))
                       + "\nFailures:\n" + "\n".join(failures))
            current_plan = self._plan(
                goal, failure_context=context,
                spec=getattr(self, "_current_spec", None))
            if not current_plan.deliverables:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="repair plan has no deliverables",
                                 attempts=attempt)
        else:
            attempt = MAX_ATTEMPTS
        files = sorted(passed) if passed else list(current_plan.deliverables)
        decision: Decision = self.gate.ask(
            "live", "Approve live execution?",
            f"Goal: {goal}\nDeliverables: {', '.join(files)}")
        approved = decision.verdict == "approved"
        if not approved:
            return RunReport("rejected", goal, current_plan, True, False,
                             files=files, attempts=attempt)
        learn_dir = self.home / "learned"
        learn_dir.mkdir(parents=True, exist_ok=True)
        record = {"goal": goal, "deliverables": files,
                  "criteria": current_plan.criteria}
        (learn_dir / "last_run.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        if self.memory is not None:
            episodic = self.memory.record_episodic(
                self.project_id, goal, {"deliverables": files})
            try:
                from .experience import build_experience, store_experience
                nodes = []
                tools_used = []
                for step in current_plan.normalized_steps():
                    nodes.append({"tool": step.tool, "args": step.args,
                                  "deliverable": step.deliverable})
                    if step.tool not in tools_used:
                        tools_used.append(step.tool)
                experience = build_experience(
                    goal, "", nodes, tools_used, [], "done",
                    "Reused workflow from a successful run.")
                store_experience(self.memory, experience)
            except Exception:
                pass
            try:
                if self.promotion_ask is not None:
                    self.memory.promote_with_ask(
                        episodic, self.promotion_ask, kind="semantic")
                elif self.memory.auto_promote_enabled():
                    self.memory.promote_to_ltm(episodic, kind="semantic")
            except Exception:
                pass
        return RunReport("done", goal, current_plan, True, True,
                         files=files, attempts=attempt)
