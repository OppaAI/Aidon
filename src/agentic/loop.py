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

import inspect
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..interface.approval import ApprovalGate, Decision
from ..system.model_client import Message, ModelClient
from ..capabilities.sandbox import Sandbox
from ..capabilities.tools import ToolRegistry
from .verify import verify_plan
from .elapsed import ElapsedTimer

MAX_ATTEMPTS = 3


@dataclass
class Step:
    tool: str = "write"
    args: dict = field(default_factory=dict)
    deliverable: str = ""
    criteria: str = ""
    executor: str = "tool"


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    needs_research: list[str] = field(default_factory=list)

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
                    criteria=str(step.get("criteria", "")),
                    executor=str(step.get("executor", "tool"))))
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
    replan_note: str = ""
    learned_dag: str = ""
    chat_reply: str = ""
    dag_summary: dict = field(default_factory=dict)
    step_lines: list[str] = field(default_factory=list)
    narration: str = ""
    delivery_summary: str = ""
    # Sandbox network state for this run: True means the
    # sandbox had network on (announced on stderr, shown on
    # approval cards), False means network off, None means
    # unknown (nothing announced, nothing shown).
    network: bool | None = None
    elapsed_seconds: float = 0.0
    approval_wait_seconds: float = 0.0


def format_duration(seconds: float | int | None) -> str:
    """Render elapsed seconds in compact human units."""
    if seconds is None:
        return ""
    total = max(0, int(round(float(seconds))))
    if total >= 3600:
        hours, remainder = divmod(total, 3600)
        return f"{hours}h {remainder // 60}m"
    minutes, remainder = divmod(total, 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def format_elapsed(report: dict) -> str:
    """Render the old-compatible elapsed footer for a report dictionary."""
    elapsed = report.get("elapsed_seconds")
    if elapsed is None:
        return ""
    line = f"\n\n⏱️ {format_duration(elapsed)}"
    waited = float(report.get("approval_wait_seconds") or 0)
    if waited >= 1:
        line += f" (plus {format_duration(waited)} waiting for you)"
    return line


def format_run_stats(report: RunReport) -> str:
    """Format the completion metrics shown beside a finished task."""
    elapsed = max(0.0, float(getattr(report, "elapsed_seconds", 0.0) or 0.0))
    elapsed_text = format_duration(elapsed)
    tokens = int(getattr(report, "model_tokens_estimated", 0) or 0)
    waited = float(getattr(report, "approval_wait_seconds", 0.0) or 0.0)
    waiting = (f" (plus {format_duration(waited)} waiting for you)"
               if waited >= 1 else "")
    return f"⏱️ {elapsed_text}{waiting} · {tokens:,} total workflow tokens (estimated)"


def run_react(client, messages: list, tools, *, max_rounds: int = 8,
              phase: str = "live", budgets=None) -> dict:
    """Optional compatibility ReAct loop for native model tool calls.

    Plan-first execution remains the default. This helper restores the old
    direct tool-call capability for integrations that provide a
    ``chat_response`` method returning ``ChatResponse.tool_calls`` (or a
    compatible response object). A JSON response containing ``tool_calls``
    is accepted as a fallback for simple model adapters.
    """
    history = list(messages or [])
    total_calls = 0
    total_rounds = 0

    def _message(role, content):
        return Message(role, str(content))

    def _call_tool(name, arguments):
        if hasattr(tools, "call"):
            return tools.call(name, arguments)
        if isinstance(tools, dict) and name in tools:
            return tools[name](**arguments) if isinstance(arguments, dict) else tools[name](arguments)
        raise ValueError(f"unknown tool: {name}")

    for _ in range(max(1, int(max_rounds))):
        total_rounds += 1
        if budgets is not None and hasattr(budgets, "add_step"):
            budgets.add_step()
        responder = getattr(client, "chat_response", None)
        if callable(responder):
            try:
                response = responder(history, phase=phase)
            except TypeError:
                response = responder(history)
        else:
            response = client.chat(history)
        total_calls += 1
        text = getattr(response, "text", response)
        calls = list(getattr(response, "tool_calls", []) or [])
        if not calls and isinstance(text, str):
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict) and decoded.get("tool_calls"):
                    from ..system.model_client import parse_openai_tool_calls
                    calls = parse_openai_tool_calls(decoded)
                    text = decoded.get("text", "")
            except (json.JSONDecodeError, TypeError):
                pass
        if not calls:
            return {"text": str(text or ""), "messages": history,
                    "tool_calls": total_calls - 1, "rounds": total_rounds}
        history.append(_message("assistant", text or ""))
        for call in calls:
            name = getattr(call, "name", "")
            if not name and isinstance(call, dict):
                name = call.get("name", "")
            arguments = getattr(call, "arguments", None)
            if arguments is None and isinstance(call, dict):
                arguments = call.get("arguments", {})
            result = _call_tool(str(name), arguments or {})
            if budgets is not None and hasattr(budgets, "add_tokens"):
                budgets.add_tokens(str(result))
            history.append(_message("tool", result))
    return {"text": "", "messages": history, "tool_calls": total_calls,
            "rounds": total_rounds, "error": "ReAct round limit exceeded"}


class GoalRunner:
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path, memory=None,
                 promotion_ask=None, project_id: str = "default",
                 plugins=None, execution_backend=None,
                 budget=None, trace=None, model_verifier=None,
                 kernel_mode: bool = False, semantic_router=None,
                 use_semantic_router_skip: bool = False,
                 run_id: str | None = None, run_control=None,
                 researcher=None, extra_tools=None, router=None,
                 versioned_writes: bool = False,
                 network_available: bool | None = None,
                 guidance: str = ""):
        self.client = client
        # Optional phase router (duck-typed RouterClient): fast
        # phases interpret/learn/respond go through it, every
        # other phase uses the default client exactly as before.
        # None means no change to any model call.
        self.router = router
        self.sandbox = sandbox
        self.gate = gate
        self.home = Path(home)
        self.memory = memory
        self.promotion_ask = promotion_ask
        self.project_id = project_id
        self.network_available = network_available
        # Optional human guidance, binding on the planner: carried
        # into the plan prompt as a guidance block (the same block
        # make_plan uses), never as a separate approval or question.
        self.guidance = guidance
        # The ToolRegistry gains the same network_available
        # parameter in a sibling change; pass it through only
        # when the registry accepts it, so this runner works
        # before and after that change lands.
        try:
            _registry_params = inspect.signature(ToolRegistry.__init__).parameters
        except (TypeError, ValueError):
            _registry_params = {}
        _registry_kwargs = ({"network_available": network_available}
                            if "network_available" in _registry_params else {})
        self.tools = ToolRegistry(sandbox.workdir, sandbox=sandbox,
                              versioned_writes=versioned_writes,
                              **_registry_kwargs)
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
        self._replan_note = ""
        self._dag_summary: dict = {}
        self.last_route = None
        self.researcher = researcher
        self.extra_tools = extra_tools or {}

    def _trace(self, kind: str, status: str, detail: str = "") -> None:
        # Attribute to this runner's own run id: sub-runners share
        # their parent's recorder, and without the override their
        # events would land in the parent's trace file. A runner
        # with no run id passes None, and record() falls back to
        # the recorder's own run id, so unregistered runs behave
        # exactly as before.
        if self.trace is not None:
            self.trace.record(kind, status, detail, run_id=self.run_id)

    def _approval_body(self, body: str) -> str:
        """Approval-card body, with the network-on line when
        the sandbox has network. Verdict logic is untouched;
        this only adds one informational line."""
        if self.network_available is True:
            return body + "\nSandbox network: on"
        return body

    def _start_mission(self, goal: str) -> None:
        """Create the durable review record for a real, cross-surface run."""
        if not self.run_id or getattr(self, "_mission_lifecycle", None) is not None:
            return
        try:
            from ..capabilities.registry_db import Registry
            from .lifecycle import MissionLifecycle
            self._mission_registry = Registry(self.home)
            self._mission_lifecycle = MissionLifecycle(self._mission_registry)
            self._mission = self._mission_lifecycle.start(
                goal, str(self.run_id), workspace=str(self.sandbox.workdir),
                guidance=self.guidance)
        except Exception as exc:
            self._mission_lifecycle = None
            self._trace("mission", "failed", f"mission start failed: {exc}")

    def _mission_prepare_review(self, goal: str, plan: Plan,
                                files: list[str], verified: list[str]) -> None:
        lifecycle = getattr(self, "_mission_lifecycle", None)
        mission = getattr(self, "_mission", None)
        if lifecycle is None or mission is None:
            return
        try:
            current = lifecycle.get(mission["id"])
            if current["state"] == "researching":
                lifecycle.transition(mission["id"], "testing", "sandbox execution started")
            criteria = [{"name": str(item), "status": "passed",
                         "evidence": "sandbox verification passed"}
                        for item in (plan.criteria or verified or files)]
            lifecycle.freeze_snapshot(
                mission["id"],
                changes=[{"path": name, "sha256": ""} for name in files],
                test_results={"passed": True, "verified": list(verified)},
                artifacts=list(files), criteria=criteria)
            from .lifecycle import NotificationOutbox
            NotificationOutbox(self._mission_registry).enqueue(
                "review_ready", "Mission ready for review",
                f"{goal}: sandbox verification passed", self.project_id)
        except Exception as exc:
            self._trace("mission", "failed", f"review snapshot failed: {exc}")

    def _mission_approval(self, approved: bool) -> None:
        lifecycle = getattr(self, "_mission_lifecycle", None)
        mission = getattr(self, "_mission", None)
        if lifecycle is None or mission is None:
            return
        try:
            state = lifecycle.get(mission["id"])["state"]
            if approved and state == "ready_for_review":
                lifecycle.transition(mission["id"], "approved", "owner approved live execution")
            elif not approved and state in ("researching", "testing", "ready_for_review"):
                lifecycle.transition(mission["id"], "rejected", "owner rejected live execution")
        except Exception as exc:
            self._trace("mission", "failed", f"mission approval update failed: {exc}")

    def _finish_mission(self, report: RunReport) -> None:
        lifecycle = getattr(self, "_mission_lifecycle", None)
        mission = getattr(self, "_mission", None)
        if lifecycle is None or mission is None:
            return
        try:
            current = lifecycle.get(mission["id"])
            if report.status != "done" and current["state"] in (
                    "researching", "testing", "ready_for_review"):
                lifecycle.transition(mission["id"], "rejected",
                                     report.error or report.status)
            lifecycle.set_report(mission["id"], report.delivery_summary or report.status)
        except Exception as exc:
            self._trace("mission", "failed", f"mission finalization failed: {exc}")
        finally:
            registry = getattr(self, "_mission_registry", None)
            if registry is not None:
                registry.close()
            self._mission_registry = None

    def _ask_for_approval(self, kind: str, title: str, body: str, **kwargs) -> Decision:
        """Ask the owner while excluding human wait from active elapsed time."""
        timer = getattr(self, "_elapsed_timer", None)
        if timer is None:
            return self.gate.ask(kind, title, body, **kwargs)
        with timer.approval_wait():
            return self.gate.ask(kind, title, body, **kwargs)

    def _chat(self, phase: str, messages) -> str:
        """One model call, router-aware.

        Fast phases (interpret, learn, respond) go through the
        router when one is supplied; all other phases, and every
        call when no router is supplied, use the default client
        exactly as before (chat_phase when it has one, else chat).
        The router is duck-typed: chat_phase(phase, messages)
        preferred, chat(phase, messages) accepted, never
        hard-required, so a plain client passed as router
        still works.
        """
        # USER.md is the owner's identity/profile. Keep it in the model
        # context for every phase; having the file on disk is not enough if
        # it is never included in the request.
        try:
            from .prompts import build_identity_prompt
            identity = build_identity_prompt(str(phase))
            messages = list(messages or [])
            if messages and getattr(messages[0], "role", "") == "system":
                messages[0] = Message(
                    "system", identity + "\n\n" + str(messages[0].content))
            else:
                messages.insert(0, Message("system", identity))
        except Exception:
            # Prompt loading must never prevent a goal from running.
            messages = list(messages or [])

        # Think effort/sampling for this phase, and the image
        # guard: a fast-phase call carrying image parts goes to
        # the strong client instead, because fast tiers are
        # text-only.
        try:
            from .think_effort import sampling_for
            sampling = sampling_for(phase)
        except Exception:
            sampling = None
        has_image = False
        for message in messages or []:
            content = getattr(message, "content", "")
            if isinstance(content, list) and any(
                    isinstance(part, dict) and part.get("type") in ("image_url", "image")
                    for part in content):
                has_image = True
        if has_image and str(phase) in ("interpret", "learn", "respond"):
            try:
                from .image_guard import image_allowed
                if not image_allowed("fast", True):
                    try:
                        return self.client.chat(messages, sampling=sampling)
                    except TypeError:
                        return self.client.chat(messages)
            except Exception:
                try:
                    return self.client.chat(messages, sampling=sampling)
                except TypeError:
                    return self.client.chat(messages)
        if self.router is not None and str(phase) in ("interpret", "learn", "respond"):
            chat_phase = getattr(self.router, "chat_phase", None)
            if callable(chat_phase):
                try:
                    return chat_phase(phase, messages, sampling=sampling)
                except TypeError:
                    return chat_phase(phase, messages)
            chat = getattr(self.router, "chat", None)
            if callable(chat):
                try:
                    return chat(phase, messages)
                except TypeError:
                    return chat(messages)
        if hasattr(self.client, "chat_phase"):
            try:
                return self.client.chat_phase(phase, messages, sampling=sampling)
            except TypeError:
                return self.client.chat_phase(phase, messages)
        try:
            return self.client.chat(messages, sampling=sampling)
        except TypeError:
            return self.client.chat(messages)

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
                decision = self._ask_for_approval("live", f"Mandatory approval: shell ({reason})", self._approval_body(f"Run shell command: {command_text}?"))
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
                    decision = self._ask_for_approval("live", f"Extra tool {name}", self._approval_body(f"Run extra tool {name} with {args}?"))
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
              spec=None, research_context: str = "") -> Plan:
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
        if self.extra_tools and not self.kernel_mode:
            # Kernel mode restricts _call_tool to seed tools, so
            # extra tools are advertised only outside kernel mode.
            system += (" Extra tools available to plan steps: "
                       + ", ".join(sorted(self.extra_tools)) + ".")
        user = goal
        # Old Aidon recalled both prior experiences and ingested knowledge
        # before planning. Keep the block bounded so memory cannot crowd
        # out the actual goal or the plan schema.
        if self.memory is not None:
            try:
                episode_limit = max(0, int(os.environ.get(
                    "AIDON_EPISODE_RECALL_LIMIT", "1") or "1"))
                knowledge_limit = max(0, int(os.environ.get(
                    "AIDON_KNOWLEDGE_RECALL_LIMIT", "3") or "3"))
                recall_tokens = max(0, int(os.environ.get(
                    "AIDON_RECALL_MAX_TOKENS", "2000") or "2000"))
                episodes, knowledge = self.memory.recall_both(
                    goal, episode_limit, knowledge_limit)
                sections = []
                for label, hits in (("Earlier experiences", episodes),
                                    ("Relevant knowledge", knowledge)):
                    texts = []
                    for hit in hits:
                        payload = getattr(getattr(hit, "point", None),
                                          "payload", {}) or {}
                        text = str(payload.get("text", "")).strip()
                        if text:
                            texts.append(text)
                    if texts:
                        sections.append("## " + label + "\n- " +
                                        "\n- ".join(texts))
                memory_text = "\n\n".join(sections)
                if recall_tokens:
                    memory_text = memory_text[:recall_tokens * 4]
                else:
                    memory_text = ""
                if memory_text:
                    user += "\n\nRelevant long-term memory (reference only; " \
                            "verify it against the current goal):\n" + memory_text
            except Exception:
                # Memory is an enhancement; a missing or unavailable store
                # must never prevent a plan from being generated.
                pass
        if spec is not None:
            spec_dict = spec.to_dict() if hasattr(spec, "to_dict") else spec
            user = (f"Goal: {goal}\n\nFrozen spec, binding for this plan, "
                    f"do not invent deliverables or constraints outside it:\n"
                    f"{json.dumps(spec_dict, indent=2)}")
        if failure_context:
            user = user + (f"\n\nPrevious attempt failed, repair only the "
                           f"failed parts, do not rewrite passed files:\n{failure_context}")
        if research_context:
            user = user + ("\n\nTargeted research findings for the plan's "
                           "open questions, use them to fill those gaps, "
                           "do not change unrelated steps:\n"
                           f"{research_context}")
        if (self.guidance or "").strip():
            # Binding human guidance, via the same guidance block
            # make_plan uses. _plan builds its prompt inline rather
            # than calling make_plan, so the block is appended here.
            from .prompts import guidance_block
            user = user + f"\n\n{guidance_block(self.guidance)}"
        self._trace("plan", "started", goal[:100])
        if self.budget is not None:
            self.budget.add_tokens(user)
        _phase = "repair" if failure_context else "plan"
        reply = self._chat(_phase, [Message("system", system), Message("user", user)])
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
                        criteria=[str(c) for c in data.get("criteria", [])],
                        needs_research=[str(x) for x in data.get("needs_research", []) or []])
        except (json.JSONDecodeError, AttributeError):
            return Plan(
                steps=[Step(tool="write", args={"file": "deliverable.md",
                                                "text": f"Goal: {goal}\n"},
                            deliverable="deliverable.md")],
                deliverables=["deliverable.md"],
                criteria=["deliverable.md exists"])

    def _research_and_replan(self, plan: Plan, goal: str,
                             spec=None) -> Plan:
        """One plan-first research + replan pass, before execution.

        When the plan names open questions (needs_research), each
        one is researched through the loop's researcher callable,
        then the planner is called exactly once more with those
        notes as context, and the replanned plan replaces the
        original. Exactly one pass, never a loop: a replanned
        plan's own needs_research is not researched again.
        No researcher means no research: the original plan is
        kept. A failed or invalid replan keeps the original
        plan and records the failure in _replan_note (copied
        onto the report by _finalize); it never fails the run.
        """
        items = [str(x) for x in (getattr(plan, "needs_research", None) or [])]
        if not items or self.researcher is None:
            return plan
        notes = []
        for item in items:
            try:
                result = self.researcher(item)
            except Exception as exc:
                notes.append(f"{item}: research failed ({exc})")
                continue
            if isinstance(result, (list, tuple)):
                notes.append(f"{item}: " + "; ".join(str(r) for r in result))
            elif isinstance(result, str):
                notes.append(f"{item}: {result}")
            else:
                notes.append(f"{item}: {result!r}")
        try:
            replanned = self._plan(
                goal, spec=spec,
                research_context="\n".join(notes))
        except Exception as exc:
            self._replan_note = (f"replan after research failed ({exc}), "
                                 "keeping the original plan")
            return plan
        if not isinstance(replanned, Plan) or (not replanned.steps
                                               and not replanned.deliverables):
            self._replan_note = ("replan after research returned an invalid "
                                 "plan (no steps or deliverables), keeping "
                                 "the original plan")
            return plan
        return replanned

    def run(self, goal: str, spec=None, research_report=None,
            live_actions=None) -> RunReport:
        self._elapsed_timer = ElapsedTimer().start()
        # Sandbox network-on announcement, one line on
        # stderr at run start. Unknown (None) or off says
        # nothing.
        if self.network_available is True:
            print("Sandbox network: on", file=sys.stderr)
        # Kernel notice, same channel: kernel mode restricts the
        # run to seed tools, so the owner sees that up front.
        if self.kernel_mode:
            from .kernel import kernel_system_note
            _kernel_note = kernel_system_note(True)
            if _kernel_note:
                print(_kernel_note, file=sys.stderr)
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
            reply = self._chat("respond", [Message("user", goal)])
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.use_semantic_router_skip and self.last_route and self.last_route[0] == "webchat" and spec is None and not _write_intent and self.researcher is not None:
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        self._start_mission(goal)
        if spec is not None:
            spec_deliverables = spec.deliverables if hasattr(spec, "deliverables") else spec.get("deliverables", [])
            try:
                plan = self._plan(goal, spec=spec)
            except Exception as exc:
                # A budget breach (or any plan failure) on the
                # initial plan call is a failed run, not a crash
                # out of the loop — same rule as the repair wrap.
                return self._finalize(RunReport("failed", goal,
                                                Plan(steps=[], deliverables=[str(d) for d in spec_deliverables], criteria=[]),
                                                False, False, error=str(exc)[:500],
                                                backend=self._backend_name()))
            if not plan.deliverables and spec_deliverables:
                plan.deliverables = list(spec_deliverables)
            plan = self._research_and_replan(plan, goal, spec=spec)
            return self._finalize(self._execute_plan(goal, plan))
        if self.memory is not None:
            try:
                from ..memory.experience import recall_experiences
                try:
                    recall_limit = max(0, int(os.environ.get(
                        "AIDON_EPISODE_RECALL_LIMIT", "1") or "1"))
                except ValueError:
                    recall_limit = 1
                experiences = recall_experiences(
                    self.memory, goal, limit=recall_limit)
                if experiences and experiences[0].dag_nodes:
                    # Adaptive DAG first: add, remove, modify, reorder,
                    # validated, with rollback to the saved graph on failure.
                    # A sequential adapt is the fallback, never a broken graph.
                    from .dag import adapt_dag
                    dag_result = adapt_dag(experiences[0], goal, client=self.client)
                    if dag_result is not None:
                        dag, summary, _model_calls = dag_result
                        try:
                            dag = dag.relativize(self.sandbox.workdir)
                        except Exception:
                            pass
                        steps = dag.to_plan_steps()
                        plan = Plan(
                            steps=steps,
                            deliverables=[str(s["deliverable"]) for s in steps if s.get("deliverable")],
                            criteria=[])
                        # Adapted plans get the same plan-first
                        # research + replan pass as fresh and
                        # spec-bound plans. Neither adapt output
                        # carries open questions today, so this
                        # is usually a no-op; if one ever does,
                        # its items are preserved onto the Plan
                        # first instead of being dropped.
                        plan.needs_research = [str(x) for x in
                                               ((summary.get("needs_research") or [])
                                                if isinstance(summary, dict) else [])]
                        plan = self._research_and_replan(plan, goal)
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
                        # Same replan routing as the DAG branch:
                        # open questions carried on adapted nodes
                        # are preserved onto the Plan, then the
                        # plan goes through research + replan.
                        plan.needs_research = [str(x) for n in nodes
                                               if isinstance(n, dict)
                                               for x in (n.get("needs_research") or [])]
                        plan = self._research_and_replan(plan, goal)
                        if plan.deliverables:
                            self._adapted = True
                            self._adapted_from = experiences[0].goal
                            return self._finalize(self._execute_plan(goal, plan))
            except Exception:
                pass
        try:
            plan = self._plan(goal)
        except Exception as exc:
            # Same rule as the spec branch above: an initial
            # plan failure (including a budget breach) fails
            # the run instead of crashing out of the loop.
            return self._finalize(RunReport("failed", goal,
                                            Plan(steps=[], deliverables=[], criteria=[]),
                                            False, False, error=str(exc)[:500],
                                            backend=self._backend_name()))
        plan = self._research_and_replan(plan, goal)
        return self._finalize(self._execute_plan(goal, plan))


    def _finalize(self, report: RunReport) -> RunReport:
        """Fill model-usage and adaptation fields, from the counting client if present."""
        timer = getattr(self, "_elapsed_timer", None)
        if timer is not None:
            timer.stop()
            report.elapsed_seconds = round(timer.elapsed_active(), 1)
            report.approval_wait_seconds = round(timer.approval_wait_seconds(), 1)
        client = self.client
        if hasattr(client, "calls") and hasattr(client, "tokens_estimated"):
            report.model_calls = int(client.calls)
            report.model_tokens_estimated = int(client.tokens_estimated)
        report.network = self.network_available
        report.adapted = bool(self._adapted)
        report.adapted_from = str(self._adapted_from)
        report.dag_summary = dict(self._dag_summary)
        report.replan_note = str(getattr(self, "_replan_note", "") or "")
        # Proven-run DAG learn, successful runs only, best
        # effort: a learn failure must never fail the run.
        if report.status == "done":
            try:
                from .dag_store import DagStore
                nodes = [{"tool": s.tool, "args": dict(s.args),
                          "deliverable": s.deliverable}
                         for s in report.plan.normalized_steps()]
                if nodes:
                    action, dag_id = DagStore(self.home).learn(
                        report.goal, nodes)
                    if dag_id:
                        report.learned_dag = f"{action}:{dag_id}"
            except Exception:
                pass
        try:
            from .autonomy_metrics import AutonomyMetrics
            AutonomyMetrics(self.home).record(
                "task_success" if report.status == "done" else "task_failed",
                run_id=str(getattr(self, "run_id", "") or ""),
                detail=report.goal[:200])
        except Exception:
            pass
        try:
            from .step_lines import render_step_lines
            report.step_lines = render_step_lines(getattr(self, "_step_lines", [])).splitlines()
        except Exception:
            pass
        try:
            from .delivery_summary import delivery_summary, problem_narration
            report.delivery_summary = delivery_summary({
                "goal": report.goal,
                "deliverables": {f: (f in report.files) for f in report.plan.deliverables},
                "files": list(report.files),
                "elapsed": report.elapsed_seconds,
                "approval_wait_seconds": report.approval_wait_seconds,
                "model_tokens_estimated": report.model_tokens_estimated,
                "repairs": max(0, report.attempts - 1)})
            if report.error:
                report.narration = problem_narration(
                    report.error, {"goal": report.goal, "attempts": report.attempts})
        except Exception:
            pass
        self._finish_mission(report)
        # Save the run state so `resume` can list and show it:
        # goal, steps, completed files, status. Best effort only;
        # a failed save must never fail the run itself.
        try:
            from .resume import save_run_state
            home = getattr(self, "home", None)
            if home is not None and getattr(self, "run_id", ""):
                save_run_state(home, str(self.run_id), {
                    "goal": report.goal,
                    "steps": [s if isinstance(s, dict) else
                              {"tool": getattr(s, "tool", ""),
                               "deliverable": getattr(s, "deliverable", "")}
                              for s in report.plan.steps],
                    "completed": list(report.files),
                    "status": report.status,
                })
        except Exception:
            pass
        return report

    def _execute_plan(self, goal: str, plan: Plan) -> RunReport:
        if self.budget is not None:
            self.budget.start()
        self._trace("interpret", "done", goal[:100])
        if self.memory is not None:
            self._trace("recall", "done", "experience recall attempted")
        from .validator import validate_plan, ALLOWED_TOOLS
        _allowed = set(ALLOWED_TOOLS) | {"llm"}
        if self.extra_tools:
            # Extra tools are dispatchable by _call_tool, so a plan
            # step naming one must pass validation here instead of
            # failing the run up front; the plan-guard cycle check
            # below reuses this same widened set.
            _allowed.update(self.extra_tools)
        if self.plugins is not None:
            try:
                _allowed.update(self.plugins.tool_names())
            except Exception:
                pass
        plan_failures = validate_plan(plan, allowed_tools=_allowed)
        if plan_failures:
            return RunReport("failed", goal, plan, False, False,
                             error="; ".join(plan_failures)[:500])
        # Plan guard, cycle check only: loop steps carry no ids,
        # so the full guard (which requires ids and args) is a
        # CLI/file check, but a depends_on cycle in a raw dict
        # plan must still fail here, not loop forever.
        try:
            from .plan_guard import validate_plan as _guard_validate
            _raw = [st for st in plan.steps if isinstance(st, dict) and st.get("depends_on")]
            if _raw:
                _guard_plan = {"steps": [
                    {"id": f"step-{i}", "tool": st.get("tool", "write"),
                     "args": st.get("args", {}), "deliverable": st.get("deliverable", ""),
                     "depends_on": st.get("depends_on", [])}
                    for i, st in enumerate(_raw)]}
                _cycle = [f for f in _guard_validate(_guard_plan, list(_allowed)) if "cycle" in f]
                if _cycle:
                    return RunReport("failed", goal, plan, False, False,
                                     error="; ".join(_cycle)[:500])
        except Exception:
            pass
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
                    if not hasattr(self, "_step_lines"):
                        self._step_lines = []
                    from .step_lines import record_attempt
                    record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "started")
                    self._trace("sandbox", "started", step.tool)
                    if step.tool == "write" and not step.args:
                        step.args = {"file": step.deliverable,
                                     "text": f"Goal: {goal}\n"}
                    if step.tool == "llm":
                        # DAG tier routing: an llm node runs on the
                        # tier it declares — fast via the respond
                        # phase, strong via the plan phase — and its
                        # reply becomes the deliverable file.
                        _prompt = str(step.args.get("prompt", "") or step.args.get("text", ""))
                        _reply = self._chat("respond" if step.executor == "fast" else "plan",
                                            [Message("user", _prompt)])
                        if step.deliverable:
                            self._call_tool("write", {"file": step.deliverable, "text": _reply})
                    else:
                        self._call_tool(step.tool, step.args)
                    from .step_lines import record_attempt
                    record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "done")
                    self._trace("sandbox", "done", step.tool)
                except Exception as exc:
                    failures.append(f"step {step.tool} failed: {exc}")
                    try:
                        from .step_lines import record_attempt
                        record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "failed", str(exc)[:100])
                    except Exception:
                        pass
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
        self._mission_prepare_review(goal, current_plan, files, sorted(verified))
        self._trace("approval", "started", "live approval requested")
        decision: Decision = self._ask_for_approval(
            "live", "Approve live execution?",
            self._approval_body(f"Goal: {goal}\nDeliverables: {', '.join(files)}"))
        self._trace("approval", decision.verdict, "live approval")
        approved = decision.verdict == "approved"
        self._mission_approval(approved)
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
