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

from ..interface.approval import ApprovalGate, Decision
from ..system.model_client import Message, ModelClient
from ..capabilities.sandbox import Sandbox
from ..capabilities.tools import ToolRegistry
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
    backend: str = "bubblewrap"
    model_calls: int = 0
    model_tokens_estimated: int = 0
    adapted: bool = False
    adapted_from: str = ""
    chat_reply: str = ""
    dag_summary: dict = field(default_factory=dict)


class GoalRunner:
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path, memory=None,
                 promotion_ask=None, project_id: str = "default",
                 plugins=None, execution_backend=None,
                 budget=None, trace=None, model_verifier=None,
                 kernel_mode: bool = False, semantic_router=None,
                 use_semantic_router_skip: bool = False,
                 run_id: str | None = None, run_control=None,
                 researcher=None, extra_tools=None):
        self.client = client
        self.sandbox = sandbox
        self.gate = gate
        self.home = Path(home)
        self.memory = memory
        self.promotion_ask = promotion_ask
        self.project_id = project_id
        self.tools = ToolRegistry(sandbox.workdir, sandbox=sandbox)
        self.plugins = plugins
        self.execution_backend = execution_backend
        self.budget = budget
        self.trace = trace
        self.model_verifier = model_verifier
        self.kernel_mode = kernel_mode
        self.semantic_router = semantic_router
        self.use_semantic_router_skip = use_semantic_router_skip
        self._adapted = False
        self._adapted_from = ""
        self.run_id = run_id
        self.run_control = run_control
        self._dag_summary: dict = {}
        self.last_route = None
        self.researcher = researcher
        self.extra_tools = extra_tools or {}

    def _trace(self, kind: str, status: str, detail: str = "") -> None:
        if self.trace is not None:
            self.trace.record(kind, status, detail)

    def _backend_name(self) -> str:
        if self.execution_backend is not None:
            return self.execution_backend.name
        return "bubblewrap"

    def _call_tool(self, name: str, args: dict) -> str:
        """Sandbox tools first, then adopted plugin tools, else unknown.

        Shell steps, when an execution backend is set, run
        through that backend, not through the sandbox
        directly, so a Docker run is recorded as Docker,
        and a Docker failure is a step failure, not a
        silent fallback to a host shell.
        """
        if self.kernel_mode:
            from .kernel import kernel_allowed
            if not kernel_allowed(name, True):
                raise ValueError(f"kernel mode: tool {name} is not a seed tool")
        if name == "shell":
            # Mandatory-approval commands always need a human,
            # even in autonomous mode: recursive delete, piped
            # download-to-shell, sudo, raw disk writes, and the
            # rest of the security gate's list.
            from ..capabilities.security_gate import command_needs_mandatory_approval
            command_text = " ".join(str(c) for c in args.get("command", [])) if isinstance(args.get("command"), list) else str(args.get("command", ""))
            reason = command_needs_mandatory_approval(command_text)
            if reason:
                decision = self.gate.ask("live", f"Mandatory approval: shell ({reason})", f"Run shell command: {command_text}?")
                if decision.verdict != "approved":
                    raise ValueError(f"shell command rejected by approval gate ({reason})")
        if name == "shell" and self.execution_backend is not None:
            command = args["command"]
            if not isinstance(command, list) or not command:
                raise ValueError("shell command must be a non-empty list")
            result = self.execution_backend.run(
                [str(c) for c in command], self.sandbox.workdir,
                timeout_seconds=self.sandbox.timeout_seconds)
            if not result.ok:
                raise ValueError(f"shell failed: {result.stderr[:200]}")
            return result.stdout
        try:
            return self.tools.call(name, args)
        except ValueError as exc:
            if str(exc).startswith("unknown tool:") and self.plugins is not None:
                try:
                    return self.plugins.call(name, args)
                except Exception:
                    pass
            if str(exc).startswith("unknown tool:") and name in self.extra_tools:
                # Extra tools are outside the confined set, so a
                # post or send tool is approval-gated here, before
                # dispatch, fail-closed on rejection.
                if any(word in name for word in ("post", "send", "publish")):
                    decision = self.gate.ask("live", f"Extra tool {name}", f"Run extra tool {name} with {args}?")
                    if decision.verdict != "approved":
                        raise ValueError(f"extra tool {name} rejected by approval gate")
                from ..capabilities.extra_tools import dispatch_extra_tool
                return dispatch_extra_tool(self.extra_tools, name, args)
            if str(exc).startswith("unknown tool:"):
                # No registered tool covers it: hand the run a
                # binding re-plan instruction instead of a bare
                # error, from the capability ladder (exact tool,
                # then skill, then approved MCP).
                from ..capabilities.capability_lookup import (
                    lookup_capability, recovery_note)
                tool_rows = {t: "" for t in self.tools.names()}
                if self.plugins is not None:
                    try:
                        for t in self.plugins.tool_names():
                            tool_rows.setdefault(t, "")
                    except Exception:
                        pass
                matches = lookup_capability(name, tools=tool_rows)
                raise ValueError(f"unknown tool: {name}\n{recovery_note(name, matches)}")
            raise

    def _plan(self, goal: str, failure_context: str = "",
              spec=None) -> Plan:
        if self.kernel_mode:
            system = ("Propose a short plan as JSON with keys steps, "
                      "deliverables, criteria. Each step is an object "
                      "with tool, args, deliverable. Tools: write, read, "
                      "list, shell only, kernel mode, no calculate, "
                      "no plugin tools. Keep every file inside "
                      "the workdir.")
        else:
            system = ("Propose a short plan as JSON with keys steps, "
                      "deliverables, criteria. Each step is an object "
                      "with tool, args, deliverable. Tools: write, read, "
                      "list, shell, calculate. Keep every file inside "
                      "the workdir.")
        if self.plugins is not None and not self.kernel_mode:
            plugin_tools = self.plugins.tool_names()
            if plugin_tools:
                system += f" Adopted plugin tools: {', '.join(plugin_tools)}."
            skill_texts = self.plugins.skill_texts()
            if skill_texts:
                system += " Skills, follow them when relevant: " + " ".join(
                    f"{name}: {text[:200]}" for name, text in skill_texts.items())
        user = goal
        if spec is not None:
            spec_dict = spec.to_dict() if hasattr(spec, "to_dict") else spec
            user = (f"Goal: {goal}\n\nFrozen spec, binding for this plan, "
                    f"do not invent deliverables or constraints outside it:\n"
                    f"{json.dumps(spec_dict, indent=2)}")
        if failure_context:
            user = user + (f"\n\nPrevious attempt failed, repair only the "
                           f"failed parts, do not rewrite passed files:\n{failure_context}")
        self._trace("plan", "started", goal[:100])
        if self.budget is not None:
            self.budget.add_tokens(user)
        _phase = "repair" if failure_context else "plan"
        if hasattr(self.client, "chat_phase"):
            reply = self.client.chat_phase(_phase, [Message("system", system), Message("user", user)])
        else:
            reply = self.client.chat([Message("system", system), Message("user", user)])
        if self.budget is not None:
            self.budget.add_tokens(reply)
        self._trace("plan", "done", "plan proposed")
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

    def run(self, goal: str, spec=None, research_report=None,
            live_actions=None) -> RunReport:
        # A frozen spec binds the planner: if one is given,
        # plan from it, do not plan fresh from the goal alone.
        # Experience reuse still applies, but only if its
        # deliverables match the spec's deliverables.
        # Research and live actions, when given, are part of
        # the run, not a separate run: research sources are
        # recorded with the run, live actions run only after
        # sandbox verification and owner approval, in
        # _execute_plan, below.
        self._current_spec = spec
        self._research_report = research_report
        self._live_actions = live_actions or []
        if self.semantic_router is not None:
            try:
                self.last_route = self.semantic_router.classify(goal)
                self._trace("interpret", "done", f"semantic route {self.last_route[0]} {self.last_route[1]:.2f}")
            except Exception:
                self.last_route = None
        # Write-intent guard, from old Aidon: the router is a hint, not a
        # safety boundary. A goal that asks for a write, a save, or a file
        # is never answered by a path with no write tools, even if the
        # router said chat, because that would silently miss the task.
        _write_intent = any(word in goal.lower() for word in ("write", "save", "create a file", "export", ".md", ".py", ".txt", ".csv"))
        if self.use_semantic_router_skip and self.last_route and self.last_route[0] == "chat" and spec is None and not _write_intent:
            reply = self.client.chat([Message("user", goal)])
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.use_semantic_router_skip and self.last_route and self.last_route[0] == "webchat" and spec is None and not _write_intent and self.researcher is not None:
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if spec is not None:
            spec_deliverables = spec.deliverables if hasattr(spec, "deliverables") else spec.get("deliverables", [])
            plan = self._plan(goal, spec=spec)
            if not plan.deliverables and spec_deliverables:
                plan.deliverables = list(spec_deliverables)
            return self._finalize(self._execute_plan(goal, plan))
        if self.memory is not None:
            try:
                from ..memory.experience import recall_experiences
                experiences = recall_experiences(self.memory, goal, limit=1)
                if experiences and experiences[0].dag_nodes:
                    # Adaptive DAG first: add, remove, modify, reorder,
                    # validated, with rollback to the saved graph on failure.
                    # A sequential adapt is the fallback, never a broken graph.
                    from .dag import adapt_dag
                    dag_result = adapt_dag(experiences[0], goal, client=self.client)
                    if dag_result is not None:
                        dag, summary, _model_calls = dag_result
                        steps = dag.to_plan_steps()
                        plan = Plan(
                            steps=steps,
                            deliverables=[str(s["deliverable"]) for s in steps if s.get("deliverable")],
                            criteria=[])
                        if plan.deliverables:
                            self._adapted = True
                            self._adapted_from = experiences[0].goal
                            self._dag_summary = summary
                            return self._finalize(self._execute_plan(goal, plan))
                    from .adapt import adapt_experience
                    adapted = adapt_experience(experiences[0], goal, client=self.client)
                    if adapted is not None:
                        nodes, _changed, _model_calls = adapted
                        plan = Plan(
                            steps=nodes,
                            deliverables=[str(n["deliverable"])
                                          for n in nodes if n.get("deliverable")],
                            criteria=[])
                        if plan.deliverables:
                            self._adapted = True
                            self._adapted_from = experiences[0].goal
                            return self._finalize(self._execute_plan(goal, plan))
            except Exception:
                pass
        plan = self._plan(goal)
        return self._finalize(self._execute_plan(goal, plan))


    def _finalize(self, report: RunReport) -> RunReport:
        """Fill model-usage and adaptation fields, from the counting client if present."""
        client = self.client
        if hasattr(client, "calls") and hasattr(client, "tokens_estimated"):
            report.model_calls = int(client.calls)
            report.model_tokens_estimated = int(client.tokens_estimated)
        report.adapted = bool(self._adapted)
        report.adapted_from = str(self._adapted_from)
        report.dag_summary = dict(self._dag_summary)
        return report

    def _execute_plan(self, goal: str, plan: Plan) -> RunReport:
        if self.budget is not None:
            self.budget.start()
        self._trace("interpret", "done", goal[:100])
        if self.memory is not None:
            self._trace("recall", "done", "experience recall attempted")
        from .validator import validate_plan, ALLOWED_TOOLS
        _allowed = set(ALLOWED_TOOLS)
        if self.plugins is not None:
            try:
                _allowed.update(self.plugins.tool_names())
            except Exception:
                pass
        plan_failures = validate_plan(plan, allowed_tools=_allowed)
        if plan_failures:
            return RunReport("failed", goal, plan, False, False,
                             error="; ".join(plan_failures)[:500])
        if not plan.deliverables:
            return RunReport("failed", goal, plan, False, False,
                             error="plan has no deliverables")
        passed: set[str] = set()
        current_plan = plan
        for attempt in range(1, MAX_ATTEMPTS + 1):
            failures: list[str] = []
            # Cancellation, checked before every attempt, and steer,
            # applied to the next repair attempt, not to passed work.
            if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                return RunReport("cancelled", goal, current_plan, False, False,
                                 files=sorted(passed), attempts=attempt,
                                 backend=self._backend_name())
            # Execute steps, skipping steps whose deliverable
            # already passed in an earlier attempt (repair rule).
            for step in current_plan.normalized_steps():
                if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                    return RunReport("cancelled", goal, current_plan, False, False,
                                     files=sorted(passed), attempts=attempt,
                                     backend=self._backend_name())
                if step.deliverable and step.deliverable in passed:
                    continue
                try:
                    if self.budget is not None:
                        self.budget.add_step()
                    self._trace("sandbox", "started", step.tool)
                    if step.tool == "write" and not step.args:
                        step.args = {"file": step.deliverable,
                                     "text": f"Goal: {goal}\n"}
                    self._call_tool(step.tool, step.args)
                    self._trace("sandbox", "done", step.tool)
                except Exception as exc:
                    failures.append(f"step {step.tool} failed: {exc}")
                    self._trace("sandbox", "failed", str(exc)[:100])
            # Verify: deliverables + plan criteria + step criteria.
            criteria = list(current_plan.criteria)
            for step in current_plan.normalized_steps():
                if step.criteria and step.criteria not in criteria:
                    criteria.append(step.criteria)
            self._trace("verify", "started", "verifying deliverables")
            ok, verify_failures, verified = verify_plan(
                self.sandbox.workdir, current_plan.deliverables, criteria)
            self._trace("verify", "done" if ok else "failed", f"verified={len(verified)}")
            failures.extend(verify_failures)
            # Model verifier, second opinion only, after code
            # verification passes. A "no" triggers repair,
            # an "unknown" reply does not override code.
            model_failed = False
            if ok and self.model_verifier is not None:
                try:
                    mv = self.model_verifier.verify(goal, current_plan.deliverables, self.sandbox.workdir)
                    self._trace("verify", mv["verdict"], f"model verifier {mv['reason'][:100]}")
                    if mv["verdict"] == "no":
                        model_failed = True
                        failures.append(f"model verifier: {mv['reason']}")
                except Exception as exc:
                    self._trace("verify", "failed", str(exc)[:100])
            # Passed means existence AND no failure mentions
            # it. A ledger file that exists but totals wrong
            # is not passed, so repair can rewrite it, while
            # a good file that passed every check is untouched.
            for name in verified:
                if not any(name in msg for msg in failures):
                    passed.add(name)
            if model_failed:
                # Model verifier said no, so passed files are
                # not trusted for repair, repair re-runs them.
                passed.clear()
            if ok and not model_failed:
                break
            if attempt == MAX_ATTEMPTS:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="; ".join(failures)[:500],
                                 attempts=attempt)
            # Repair: ask for a repaired plan, passed files untouched.
            steer_notes = self.run_control.steer_notes(self.run_id) if self.run_control is not None else []
            context = ("Passed deliverables (do not rewrite): "
                       + ", ".join(sorted(passed))
                       + "\nFailures:\n" + "\n".join(failures))
            if steer_notes:
                context += "\nSteer notes from the owner, apply to the repair:\n" + "\n".join(steer_notes)
            try:
                current_plan = self._plan(
                    goal, failure_context=context,
                    spec=getattr(self, "_current_spec", None))
            except Exception as exc:
                # Budget exceeded, or a plan failure, is a
                # failed run, not a crash out of the loop.
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error=str(exc)[:500],
                                 attempts=attempt,
                                 backend=self._backend_name())
            if not current_plan.deliverables:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="repair plan has no deliverables",
                                 attempts=attempt)
        else:
            attempt = MAX_ATTEMPTS
        files = sorted(passed) if passed else list(current_plan.deliverables)
        self._trace("approval", "started", "live approval requested")
        decision: Decision = self.gate.ask(
            "live", "Approve live execution?",
            f"Goal: {goal}\nDeliverables: {', '.join(files)}")
        self._trace("approval", decision.verdict, "live approval")
        approved = decision.verdict == "approved"
        if not approved:
            return RunReport("rejected", goal, current_plan, True, False,
                             files=files, attempts=attempt,
                             backend=self._backend_name())
        # Live execution, only after sandbox verification
        # and owner approval, with live verification and
        # rollback on failure. No live actions, no live step.
        self._trace("live", "started", "live execution")
        live_record = None
        if getattr(self, "_live_actions", None):
            from ..capabilities.live import LiveExecutor
            executor = LiveExecutor(self.sandbox.workdir, self.home)
            live_record = executor.execute(self._live_actions)
            if not executor.verify_live(live_record):
                executor.rollback(live_record)
                return RunReport("failed", goal, current_plan, True, False,
                                 files=files, attempts=attempt,
                                 error=f"live verification failed: {live_record.error}",
                                 backend=self._backend_name())
        learn_dir = self.home / "learned"
        learn_dir.mkdir(parents=True, exist_ok=True)
        record = {"status": "done", "project_id": self.project_id,
                  "goal": goal, "deliverables": files,
                  "criteria": current_plan.criteria,
                  "backend": self._backend_name(),
                  "image": getattr(self.execution_backend, "image", "")
                  if self.execution_backend is not None else "",
                  "research": (self._research_report.to_dict()
                               if getattr(self, "_research_report", None) is not None else None),
                  "live": (live_record.to_dict() if live_record is not None else None)}
        (learn_dir / "last_run.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        if self.memory is not None:
            episodic = self.memory.record_episodic(
                self.project_id, goal, {"deliverables": files})
            try:
                from ..memory.experience import build_experience, store_experience
                nodes = []
                tools_used = []
                for step in current_plan.normalized_steps():
                    nodes.append({"tool": step.tool, "args": step.args,
                                  "deliverable": step.deliverable})
                    if step.tool not in tools_used:
                        tools_used.append(step.tool)
                # Incremental improving: the lesson for the
                # next run includes research sources and live
                # verification, not just the workflow shape,
                # so the next run starts from what actually
                # worked, what was verified, and what went
                # live, instead of starting from scratch.
                lesson_parts = ["Reused workflow from a successful run."]
                if getattr(self, "_research_report", None) is not None:
                    lesson_parts.append(
                        f"Research: {len(self._research_report.verified_facts)} verified facts, "
                        f"{len(self._research_report.unverified_claims)} unverified claims, "
                        f"sources: {', '.join(s.url for s in self._research_report.sources)}")
                if live_record is not None:
                    lesson_parts.append(
                        f"Live: verified={live_record.verified}, outputs={', '.join(live_record.outputs)}")
                from .adapt import generalize_nodes as _generalize_nodes
                _gen_nodes = _generalize_nodes(nodes, goal)
                experience = build_experience(
                    goal, "", nodes, tools_used, [], "done",
                    " ".join(lesson_parts))
                experience.generalized_nodes = _gen_nodes
                store_experience(self.memory, experience)
                try:
                    from ..memory.memory_extras import rolling_summary as _rolling_summary
                    _rolling_summary(self.home, self.project_id, goal, files, "done")
                except Exception:
                    pass
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
        self._trace("learn", "done", "experience recorded")
        return RunReport("done", goal, current_plan, True, True,
                         files=files, attempts=attempt,
                         backend=self._backend_name())
