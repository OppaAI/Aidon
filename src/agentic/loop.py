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

Memory, blank by design: a new home starts with empty
collections, no import from previous projects. When a
memory is present, the loop recalls Experiences before
planning fresh, records episodic memory scoped by
project_id, and builds an Experience after a successful
run only. Promotion to long-term memory is automatic:
a done run's episodic record and a chat exchange are
copied to LTM, deduplicated when identical and
superseding when the same shape recurs.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..interface.approval import ApprovalGate, Decision
from ..system.model_client import Message, ModelClient
from ..capabilities.sandbox import Sandbox
from ..capabilities.tools import ToolRegistry
from .verify import verify_plan
from .elapsed import ElapsedTimer

MAX_ATTEMPTS = 3
MAX_REPLANS = 1
MAX_PLAN_REPAIRS = 2
# Full skill text budget per skill on a level-2 load: about
# 5000 tokens, so one huge skill file cannot flood the plan
# prompt. The head (200 chars) stays the always-on level 1.
_SKILL_FULL_CHARS = 20000


def _resolve_count(env_name: str, default: int, minimum: int) -> int:
    """Resolve a "how many times" knob: env var, else default, clamped.

    Invalid values fall back to the default, never to 0 for
    attempts (a run with zero attempts could never execute)
    or below the given minimum. Settings and .env.example
    carry the same names; the loop reads them per run, so a
    changed value applies to the next run.
    """
    try:
        value = int(os.environ.get(env_name, "") or default)
    except ValueError:
        return default
    return max(minimum, value)


@dataclass
class Step:
    tool: str = "write"
    args: dict = field(default_factory=dict)
    deliverable: str = ""
    criteria: str = ""
    executor: str = "tool"
    # DAG identity: steps from a DAG carry the node id and its
    # declared dependencies, so the executor can schedule
    # independent steps concurrently. Free-form model plans
    # leave these empty and always run sequentially: an
    # undeclared dependency must never be mistaken for
    # independence.
    id: str = ""
    depends_on: list[str] = field(default_factory=list)


# Tool-name fragments that mean the step leaves the workdir, touches a
# third party, spends money, deletes data, or installs code. A plan that
# contains any of them is not reversible, so a "live" pre-approval must
# not cover it. Conservative on purpose: a false "not reversible" only
# means one more card, a false "reversible" would skip one it should not.
SIDE_EFFECT_TOOL_WORDS = (
    "send", "post", "publish", "email", "mail", "install", "adopt",
    "delete", "spend", "pay", "upload", "record", "tts", "speak",
    "mcp_", "sam_", "transcribe", "threads", "instagram", "slack",
    "whatsapp", "notify", "schedule_job", "cancel_schedule",
)


def plan_is_reversible(plan: Plan, allowed_tools=None) -> bool:
    """True when every step stays inside the run's own workdir.

    Used by the pre-approval gate: a pre-approved "live" card is
    honoured only for a plan that cannot send, publish, spend,
    delete, or install anything. allowed_tools names steps a
    caller has already made safe — practice tools, whose
    destinations are forced to the owner and whose files are
    confined to the practice folder — so they do not trip
    the trigger-word check.
    """
    allowed = {str(t).lower() for t in (allowed_tools or ())}
    for step in plan.normalized_steps():
        name = str(getattr(step, "tool", "") or "").lower()
        if name in allowed:
            continue
        if any(word in name for word in SIDE_EFFECT_TOOL_WORDS):
            return False
    return True


@dataclass
class Plan:
    steps: list = field(default_factory=list)
    deliverables: list[str] = field(default_factory=list)
    criteria: list[str] = field(default_factory=list)
    needs_research: list[str] = field(default_factory=list)
    # True only for plans built from a DAG, where every
    # dependency is declared and undeclared means
    # independent. The executor runs such a plan's levels
    # concurrently; every other plan runs sequentially.
    parallel_ok: bool = False
    # Skill names the planner asked to have loaded in full
    # (level 2). Read by _plan for one expansion pass, then
    # ignored: the expanded plan's own list never triggers
    # a further pass.
    skills: list = field(default_factory=list)

    def normalized_steps(self) -> list[Step]:
        out = []
        for index, step in enumerate(self.steps):
            if isinstance(step, Step):
                if not step.id:
                    step.id = f"step-{index}"
                out.append(step)
            elif isinstance(step, dict):
                out.append(Step(
                    tool=str(step.get("tool", "write")),
                    args=dict(step.get("args", {})),
                    deliverable=str(step.get("deliverable", "")),
                    criteria=str(step.get("criteria", "")),
                    executor=str(step.get("executor", "tool")),
                    id=str(step.get("id", "") or f"step-{index}"),
                    depends_on=[str(d) for d in step.get("depends_on", [])]
                    if isinstance(step.get("depends_on", []), list) else []))
            else:
                out.append(Step(tool="write", args={"file": "deliverable.md",
                                                    "text": str(step)},
                                deliverable="deliverable.md",
                                id=f"step-{index}"))
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
    # Stopped Ask/Plan run: {"stop_id", "mode", "goal", ...} when
    # this report is a stop, else {}. The surface turns it into
    # the Proceed button (WebUI) or the proceed <id> reply text.
    mode_stop: dict = field(default_factory=dict)
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
    """Render the compatible elapsed footer for a report dictionary."""
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


def run_react(client, messages: list, tools, *, max_rounds: int | None = None,
              phase: str = "live", budgets=None, max_workers: int | None = None) -> dict:
    """Optional compatibility ReAct loop for native model tool calls.

    Plan-first execution remains the default. This helper restores the old
    direct tool-call capability for integrations that provide a
    ``chat_response`` method returning ``ChatResponse.tool_calls`` (or a
    compatible response object). A JSON response containing ``tool_calls``
    is accepted as a fallback for simple model adapters.

    Rounds are sequential by nature: each round's model call
    depends on the previous round's tool results. Tool calls
    within one round run concurrently (AIDON_REACT_MAX_WORKERS,
    default 3): the model emits them without knowing any
    result, so they are independent by construction, and
    results are appended in call order, never completion
    order, like swarm's merge. A failing call propagates
    like the sequential version, though already-started
    calls finish first. Tools that share mutable state
    should use max_workers=1. Round count is
    AIDON_REACT_MAX_ROUNDS (default 8).
    """
    history = list(messages or [])
    total_calls = 0
    total_rounds = 0
    rounds = (_resolve_count("AIDON_REACT_MAX_ROUNDS", 8, 1)
              if max_rounds is None else max(1, int(max_rounds)))
    workers = (_resolve_count("AIDON_REACT_MAX_WORKERS", 3, 1)
               if max_workers is None else max(1, int(max_workers)))

    def _message(role, content):
        return Message(role, str(content))

    def _call_tool(name, arguments):
        if hasattr(tools, "call"):
            return tools.call(name, arguments)
        if isinstance(tools, dict) and name in tools:
            return tools[name](**arguments) if isinstance(arguments, dict) else tools[name](arguments)
        raise ValueError(f"unknown tool: {name}")

    def _run_call(call):
        name = getattr(call, "name", "")
        if not name and isinstance(call, dict):
            name = call.get("name", "")
        arguments = getattr(call, "arguments", None)
        if isinstance(call, dict) and arguments is None:
            arguments = call.get("arguments", {})
        return _call_tool(str(name), arguments or {})

    for _ in range(rounds):
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
        if len(calls) > 1 and workers > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(workers, len(calls))) as pool:
                results = list(pool.map(_run_call, calls))
        else:
            results = [_run_call(call) for call in calls]
        for result in results:
            if budgets is not None and hasattr(budgets, "add_tokens"):
                budgets.add_tokens(str(result))
            history.append(_message("tool", result))
    return {"text": "", "messages": history, "tool_calls": total_calls,
            "rounds": total_rounds, "error": "ReAct round limit exceeded"}


class GoalRunner:
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path, memory=None,
                 project_id: str = "default",
                 plugins=None, execution_backend=None,
                 budget=None, trace=None, model_verifier=None,
                 kernel_mode: bool = False, semantic_router=None,
                 use_semantic_router_skip: bool = False,
                 run_id: str | None = None, run_control=None,
                 researcher=None, extra_tools=None, router=None,
                 versioned_writes: bool = False,
                 network_available: bool | None = None,
                 guidance: str = "", image_paths=None, force_web: bool = False,
                 stream_callback=None, conversation_context: str = "",
                 route_callback=None, trace_callback=None,
                 allowed_side_effect_tools=None, gate_context=None,
                 think_max: bool = False, research_mode: bool = False,
                 deep_researcher=None, deadline_seconds: float | None = None,
                 mode: str | None = None, question_ask=None,
                 conversation_key: str = "", resume_plan=None):
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
        self.project_id = project_id
        self.network_available = network_available
        # Optional human guidance, binding on the planner: carried
        # into the plan prompt as a guidance block (the same block
        # make_plan uses), never as a separate approval or question.
        self.guidance = guidance
        self.image_paths = [Path(path) for path in (image_paths or [])]
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
        self.allowed_side_effect_tools = frozenset(
            str(t).lower() for t in (allowed_side_effect_tools or ()))
        # Run context handed to approval cards: run_id,
        # conversation_id, source, and a WhatsApp destination
        # when the run warrants one. None means no context.
        self.gate_context = gate_context
        self.budget = budget
        self.trace = trace
        self._trace_started = {}
        self.model_verifier = model_verifier
        self._effort_escalated = False
        if self.model_verifier is not None:
            self.model_verifier.effort_escalated = False
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
        self.force_web = bool(force_web)
        # /think or the 🧠 button: every phase at maximum thinking
        # for this run (reasoning_effort max on backends that take
        # it, think=True on boolean backends).
        self.think_max = bool(think_max)
        # /research mode: deep research first, at max thinking, for
        # this run. research_mode implies think_max (see _chat);
        # deep_researcher is the injected loop-side deep research
        # callable (query, chat) -> report dict, built by main.py
        # from the configured search backend. None means the mode
        # still max-thinks, but research stays the standard kind.
        self.research_mode = bool(research_mode)
        self.deep_researcher = deep_researcher
        self._deep_research_context = ""
        # Ask/Plan/Execute mode. None means undecided: the interpret
        # pass may detect Ask/Plan from the goal, else Execute.
        # A chosen mode ("ask", "plan", "execute") always wins over
        # detection. question_ask is the chat callback the Ask
        # interview asks through (no clock, no card); None means
        # Ask mode cannot interview and fails the run instead.
        # conversation_key routes a bare proceed to this run's stop.
        # resume_plan skips planning: the proceed of a Plan stop
        # executes the saved plan directly.
        self.mode = mode if mode in ("ask", "plan", "execute") else None
        self.question_ask = question_ask
        self.conversation_key = str(conversation_key or "")
        self.resume_plan = resume_plan
        # Wall-clock time box for the run, seconds from run()
        # start; None means unlimited. Enforced between execution
        # steps (see _deadline_exceeded), never mid-step.
        self.deadline_seconds = deadline_seconds
        self.stream_callback = stream_callback
        self.conversation_context = str(conversation_context or "")
        self.route_callback = route_callback
        self.trace_callback = trace_callback

    def _counting_clients(self) -> list:
        """Counting clients whose phase deltas feed trace events.

        The main client plus the router's tier clients. Taking
        a delta twice from the same object is harmless (the
        second take is zero, drained), so a shared client
        cannot double-count.

        Routers hide their tiers: a RoutedClient keeps them as
        fast_client/strong_client and a RouterClient keeps them
        in clients/default, so unwrap one level or the trace
        drains nothing and every phase honestly shows 0→0.
        """
        clients: list = []
        candidates = [getattr(self, "client", None),
                      getattr(self, "router", None)]
        expanded = list(candidates)
        for candidate in candidates:
            if candidate is None:
                continue
            expanded.append(getattr(candidate, "fast_client", None))
            expanded.append(getattr(candidate, "strong_client", None))
            tier_map = getattr(candidate, "clients", None)
            if isinstance(tier_map, dict):
                expanded.extend(tier_map.values())
            expanded.append(getattr(candidate, "default", None))
        for candidate in expanded:
            if candidate is not None and \
                    callable(getattr(candidate, "take_phase_usage", None)) \
                    and candidate not in clients:
                clients.append(candidate)
        return clients

    def _trace(self, kind: str, status: str, detail: str = "") -> None:
        # Attribute to this runner's own run id: sub-runners share
        # their parent's recorder, and without the override their
        # events would land in the parent's trace file. A runner
        # with no run id passes None, and record() falls back to
        # the recorder's own run id, so unregistered runs behave
        # exactly as before.
        # Seconds are measured between a kind's "started" event
        # and its terminal event. Tokens drain
        # from the counting clients on terminal events only: a
        # "started" event precedes its calls, so it carries
        # zeros. Non-model steps (sandbox, checkpoint) drain
        # nothing and honestly show 0→0.
        started = getattr(self, "_trace_started", None)
        if started is None:
            started = self._trace_started = {}
        seconds = 0.0
        tokens_in = tokens_out = thinking_tokens = 0
        thinking = ""
        if status == "started":
            started[kind] = time.monotonic()
        else:
            begin = started.pop(kind, None)
            if begin is not None:
                seconds = round(time.monotonic() - begin, 1)
            for client in self._counting_clients():
                usage = client.take_phase_usage(kind)
                tokens_in += usage["tokens_in"]
                tokens_out += usage["tokens_out"]
                thinking_tokens += usage["thinking_tokens"]
                if usage["thinking"]:
                    thinking = usage["thinking"]
        if self.trace is not None:
            event = self.trace.record(kind, status, detail, run_id=self.run_id)
            if callable(self.trace_callback):
                payload = {
                    "phase": kind, "status": status, "title": detail,
                    "detail": detail, "seconds": seconds,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "thinking_tokens": thinking_tokens,
                }
                if thinking:
                    payload["thinking"] = thinking
                self.trace_callback(payload)
        if self.run_control is not None and self.run_id:
            stage_map = {
                "interpret": "interpret", "research": "research",
                "recall": "recall", "plan": "plan", "sandbox": "sandbox",
                "verify": "sandbox", "approval": "approval",
                "live": "live", "learn": "learn",
            }
            stage = stage_map.get(kind)
            if stage:
                self.run_control.set_stage(self.run_id, stage)

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

    def _save_checkpoint(self, phase: str, goal: str, plan, passed, verified=None,
                         attempt: int = 1) -> None:
        store = self._checkpoint_store()
        if store is None:
            return
        try:
            from .checkpoint import RunCheckpoint, plan_fingerprint, plan_to_dict
            store.save(RunCheckpoint(
                run_id=str(self.run_id),
                goal=goal,
                phase=phase,
                plan=plan_to_dict(plan),
                passed=sorted(set(passed or [])),
                verified=sorted(set(verified or [])),
                attempt=attempt,
                plan_fingerprint=plan_fingerprint(plan),
            ))
        except Exception as exc:
            self._trace("checkpoint", "failed", f"save failed: {exc}")

    def _load_checkpoint(self, workdir, goal: str = "", plan=None):
        store = self._checkpoint_store()
        if store is None:
            return None
        try:
            from .checkpoint import filter_resumable_passed
            if goal:
                cp = store.load_for_goal(str(self.run_id), goal, plan=plan)
            else:
                cp = store.load(str(self.run_id))
            if cp is None:
                return None
            cp.passed = filter_resumable_passed(cp.passed, workdir)
            return cp
        except Exception as exc:
            self._trace("checkpoint", "failed", f"load failed: {exc}")
            return None

    def _clear_checkpoint(self) -> None:
        store = self._checkpoint_store()
        if store is None:
            return
        try:
            store.clear(str(self.run_id))
        except Exception:
            pass

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

    def _checkpoint_store(self):
        if not self.run_id:
            return None
        try:
            from ..capabilities.registry_db import Registry
            from .checkpoint import CheckpointStore
            if getattr(self, "_checkpoint_registry", None) is None:
                self._checkpoint_registry = Registry(self.home)
            return CheckpointStore(self._checkpoint_registry)
        except Exception:
            return None
  
    def _ask_for_approval(self, kind: str, title: str, body: str, **kwargs) -> Decision:
        """Ask the owner while excluding human wait from active elapsed time."""
        # Natural framing, varied per approval; the title and body keep
        # their exact content, so the owner still sees what is asked.
        # The framing comes from the fast respond phase when one is
        # configured (a small model writes one sentence), else from
        # the rotation. Facts stay in the body, never in the model.
        from ..interface.phrasing import approval_lead_in, phrase_with_model
        lead = phrase_with_model(
            "approval",
            lambda prompt: self._chat("respond", [Message("user", prompt)]))
        body = f"{lead or approval_lead_in()}\n\n{body}"
        # Run context for the deciding surface: which run and
        # conversation this card belongs to, and where a copy
        # goes (e.g. WhatsApp). Gates that do not understand
        # context ignore it; the WebUI gate delivers from it.
        if "context" not in kwargs and getattr(self, "gate_context", None):
            kwargs["context"] = self.gate_context
        timer = getattr(self, "_elapsed_timer", None)
        if timer is None:
            return self.gate.ask(kind, title, body, **kwargs)
        with timer.approval_wait():
            return self.gate.ask(kind, title, body, **kwargs)

    def _route_judge(self):
        """Fast-model second opinion for routing, or None.

        Only when the loop's client is a RoutedClient (i.e. a
        fast model is configured): the judge calls the fast
        tier, never the strong one. Without a fast model there
        is no judge and routing stays embedding-only. The judge
        returns a route or None (abstain); any failure abstains,
        so routing never breaks on a judge error.
        """
        fast = getattr(getattr(self, "client", None), "fast_client", None)
        if fast is None:
            return None

        def judge(text):
            prompt = (
                "Classify this user message as exactly one of: "
                "chat, webchat, agentic, artifact.\n"
                "chat = chatting, asking a question, or talking about "
                "something, including telling the assistant to wait "
                "while the user checks something themselves.\n"
                "webchat = asking to look something up on the web.\n"
                "agentic = asking the assistant to DO something, change "
                "state, or produce a file.\n"
                "artifact = asking for a document or artifact to keep.\n"
                "Reply with only JSON like {\"route\": \"chat\"}.\n"
                f"Message: {text}")
            try:
                try:
                    reply = fast.chat([Message("user", prompt)],
                                      phase="route")
                except TypeError:
                    reply = fast.chat([Message("user", prompt)])
            except Exception:
                return None
            try:
                route = str(json.loads(reply).get("route", "")).strip().lower()
            except Exception:
                route = ""
            if route not in ("chat", "webchat", "agentic", "artifact"):
                match = re.search(
                    r'"route"\s*:\s*"(chat|webchat|agentic|artifact)"',
                    str(reply).lower())
                if match:
                    route = match.group(1)
                elif str(reply).strip().lower() in (
                        "chat", "webchat", "agentic", "artifact"):
                    route = str(reply).strip().lower()
                else:
                    return None
            return route

        return judge

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
            sampling = sampling_for(
                phase,
                escalate=self._effort_escalated and str(phase) in ("repair", "verify"),
                creative=bool(getattr(self, "_creative_mode", False)),
                think_max=bool(getattr(self, "think_max", False) or
                               getattr(self, "research_mode", False)))
        except Exception:
            sampling = None
        if (self.stream_callback is not None
                and str(phase).lower() in ("respond", "webchat")):
            stream_client = self.router if self.router is not None else self.client
            stream = getattr(stream_client, "chat_stream", None)
            if callable(stream):
                chunks = []
                try:
                    if self.router is not None:
                        iterator = stream(str(phase), messages, sampling=sampling)
                    else:
                        try:
                            iterator = stream(messages, sampling=sampling,
                                              phase=str(phase))
                        except TypeError:
                            iterator = stream(messages, sampling=sampling)
                except TypeError:
                    try:
                        iterator = (stream(str(phase), messages)
                                    if self.router is not None
                                    else stream(messages))
                    except TypeError:
                        iterator = stream(messages)
                for chunk in iterator:
                    text = str(chunk)
                    if text:
                        chunks.append(text)
                        self.stream_callback(text)
                return "".join(chunks)
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
            # rest of the security gate's list. The kind is
            # "mandatory", not "live": a pre-approval granted for a
            # reversible live run must never cover one of these.
            from ..capabilities.security_gate import command_needs_mandatory_approval
            command_text = " ".join(str(c) for c in args.get("command", [])) if isinstance(args.get("command"), list) else str(args.get("command", ""))
            reason = command_needs_mandatory_approval(command_text)
            if reason:
                decision = self._ask_for_approval(
                    "mandatory", f"Mandatory approval: shell ({reason})",
                    self._approval_body(f"Run shell command: {command_text}?"),
                    force_ask=True,
                )
                if decision.verdict not in ("approved", "auto_approved"):
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
        # Normalize harmless model naming variations at the execution
        # boundary. Deterministic validation still rejects missing paths or
        # empty shell commands before they reach this point.
        if isinstance(args, dict):
            args = dict(args)
            if name in ("write", "write_file"):
                if not args.get("file"):
                    for alias in ("name", "path", "input_path", "filename"):
                        if args.get(alias):
                            args["file"] = args[alias]
                            break
                if "text" not in args and "content" in args:
                    args["text"] = args["content"]
            elif name == "shell" and isinstance(args.get("command"), str):
                import shlex
                args["command"] = shlex.split(args["command"])
        try:
            if name == "shell" and isinstance(args, dict):
                command_text = " ".join(str(x) for x in
                                        (args.get("command") or []))
                # In kernel mode generate_document is NOT available
                # (seed tools only), so the guard must not point at
                # it: it points at the plugin ladder instead. A
                # misleading "use the available tool" message here
                # sent a real run in circles.
                if self.kernel_mode:
                    _pdf_note = ("Kernel mode: generate_document is not "
                                 "available here (seed tools only). Earn "
                                 "the capability via the plugin ladder: "
                                 "research, draft a plugin or skill, "
                                 "sandbox-test it, adopt it with owner "
                                 "approval.")
                else:
                    _pdf_note = ("use the available generate_document "
                                 "tool instead")
                if ("fpdf" in command_text.lower() and
                        "generate_document" in self.extra_tools):
                    raise ValueError(
                        "Do not use shell/fpdf for PDF output; "
                        f"{_pdf_note}")
                if "generate_document" in self.extra_tools:
                    # The text check above misses the common bypass:
                    # write a script file importing fpdf, then run it
                    # by name (``python3 make_pdf.py`` contains no
                    # "fpdf"). Refuse to run a workdir script that
                    # imports a PDF library when the document tool
                    # already writes PDFs with no install needed.
                    import re as _re
                    workdir = getattr(self.sandbox, "workdir", None)
                    for token in (args.get("command") or []):
                        if not str(token).endswith(".py"):
                            continue
                        try:
                            candidate = Path(str(token))
                            if not candidate.is_absolute():
                                candidate = Path(workdir) / candidate
                            elif workdir is not None:
                                # Only police scripts inside the run's
                                # workdir; host scripts are not the
                                # planner's PDF bypass.
                                resolved = candidate.resolve()
                                root = Path(workdir).resolve()
                                if resolved != root and root not in resolved.parents:
                                    continue
                            source = candidate.read_text(
                                encoding="utf-8", errors="replace")
                        except Exception:
                            continue
                        if _re.search(r"^\s*(?:import|from)\s+(fpdf|reportlab)\b",
                                      source, _re.MULTILINE):
                            raise ValueError(
                                "Do not run a script importing fpdf or "
                                "reportlab for PDF output; "
                                f"{_pdf_note}")
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
                    decision = self._ask_for_approval(
                        "live", f"Extra tool {name}",
                        self._approval_body(f"Run extra tool {name} with {args}?"),
                        action_class="publish",   # can graduate after threshold
                    )
                    if decision.verdict not in ("approved", "auto_approved"):
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
              spec=None, research_context: str = "",
              skill_full: dict | None = None) -> Plan:
        if self.kernel_mode:
            system = ("Propose a short plan as JSON with keys steps, "
                      "deliverables, criteria. Each step is an object "
                      "with tool, args, deliverable. Tools: write, read, "
                      "list, shell only, kernel mode, no calculate, "
                      "no plugin tools. Keep every file inside "
                      "the workdir.")
        else:
            system = ("Propose a short plan as JSON with keys steps, "
                      "deliverables, criteria, skills. Each step is an object "
                      "with tool, args, deliverable. Tools: write, read, "
                      "list, shell, calculate. Keep every file inside "
                      "the workdir.")
        system += (" Every item in deliverables MUST be produced by exactly "
                   "one step; never return deliverables with an empty or "
                   "missing steps array. For GUI requests, use the "
                   "computer_use tool when it is listed as available.")
        if self.plugins is not None and not self.kernel_mode:
            plugin_tools = self.plugins.tool_names()
            if plugin_tools:
                system += f" Adopted plugin tools: {', '.join(plugin_tools)}."
            skill_texts = self.plugins.skill_texts()
            if skill_texts:
                system += " Skills, follow them when relevant: " + " ".join(
                    f"{name}: {text[:200]}" for name, text in skill_texts.items())
                system += (" Grounding rule: skills are guidance text, "
                           "not callable tools — never put a skill name "
                           "in a step's tool field, and never invent a "
                           "skill name outside this list.")
                if skill_full is None:
                    system += (" If a skill above holds the procedure "
                               "this goal needs, list its name in the "
                               "plan JSON 'skills' array; the full text "
                               "will be loaded and planning will repeat "
                               "with it.")
                else:
                    for full_name, full_text in skill_full.items():
                        body = str(full_text)[:_SKILL_FULL_CHARS]
                        system += (f" Full skill text for {full_name}, "
                                   f"loaded on your request, follow it: {body}")
                        if len(str(full_text)) > _SKILL_FULL_CHARS:
                            system += " [cut here at the full-text budget]"
        if self.extra_tools and not self.kernel_mode:
            # Kernel mode restricts _call_tool to seed tools, so
            # extra tools are advertised only outside kernel mode.
            system += (" Extra tools available to plan steps: "
                       + ", ".join(sorted(self.extra_tools)) + ".")
            if "schedule_job" in self.extra_tools:
                system += (" schedule_job takes name, kind, schedule, goal, "
                           "and optional until (ISO datetime, the job "
                           "never fires after it), tz (IANA timezone "
                           "name for interpreting the schedule), and "
                           "time_limit (per-run seconds, 0 = unlimited; "
                           "a run cut by it ends partial with files "
                           "kept). Schedules that fire: at:<ISO>, "
                           "daily HH:MM, weekly DAY HH:MM, monthly ..., "
                           "or a 5-field cron — never 'every N …' or a "
                           "bare ISO datetime, which never fire.")
            if "computer_use" in self.extra_tools:
                system += (" For GUI tasks such as opening an application or "
                           "typing into it, use computer_use with validated "
                           "click/type/key actions; do not substitute a "
                           "file-only plan.")
            if "generate_document" in self.extra_tools:
                system += (" For PDF or document requests, call "
                            "generate_document directly. Do not use shell "
                            "to import fpdf, reportlab, or install a PDF "
                            "library; the document tool already writes the "
                            "requested output file.")
                system += (" Document and file tools refuse to overwrite an "
                           "existing path unless overwrite=true is supplied "
                           "and the human approves it. When the path exists, "
                           "they write to a numbered rename (name_2, "
                           "name_3, ...) and report the actual path; use "
                           "that reported path in deliverables and "
                           "criteria, and do not retry the original path.")
                system += (" Do not invent a filename or the literal "
                           "workdir/ prefix. If the user did not specify a "
                           "filename, omit the output path and let Aidon "
                           "derive a contextual timestamped filename.")
            if "install_python_package" in self.extra_tools:
                system += (" If a genuinely required optional Python package "
                           "is unavailable and no built-in capability can "
                           "do the job, use install_python_package in an "
                           "isolated mission venv. Never install packages "
                           "through shell, pip, or sudo; installation is "
                           "approval-gated.")
        system += (" Discover before building: the tools listed above are "
                   "what you can call. If a listed tool does the job, use "
                   "it; do not write a replacement script, plugin, or tool "
                   "for it. If a listed tool fails, diagnose the error and "
                   "try another listed tool before building a replacement. "
                   "Before writing code that imports a package, confirm the "
                   "package is already available; never write code that "
                   "depends on a package you have not confirmed.")
        user = goal
        # Recall both prior experiences and ingested knowledge.
        # before planning. Keep the block bounded so memory cannot crowd
        # out the actual goal or the plan schema.
        if self.memory is not None:
            try:
                episode_limit = max(0, int(os.environ.get(
                    "AIDON_EPISODE_RECALL_LIMIT", "1") or "1"))
                knowledge_limit = max(0, int(os.environ.get(
                    "AIDON_KNOWLEDGE_RECALL_LIMIT", "3") or "3"))
                semantic_limit = max(0, int(os.environ.get(
                    "AIDON_SEMANTIC_RECALL_LIMIT", "3") or "3"))
                recall_tokens = max(0, int(os.environ.get(
                    "AIDON_RECALL_MAX_TOKENS", "2000") or "2000"))
                episodes, knowledge = self.memory.recall_both(
                    goal, episode_limit, knowledge_limit)
                # General facts (extracted + promoted semantic points)
                # are a third recall leg, recalled the same full way
                # as everything else: hybrid dense + sparse keyword
                # search fused by RRF, retention-ranked by extraction
                # importance, plus entity-graph node expansion.
                semantic = self.memory.recall(
                    "semantic", goal, limit=semantic_limit) if semantic_limit else []
                sections = []
                for label, hits in (("Earlier experiences", episodes),
                                    ("Relevant knowledge", knowledge),
                                    ("General facts", semantic)):
                    texts = []
                    for hit in hits:
                        point = getattr(hit, "point", None)
                        payload = getattr(point, "payload", {}) or {}
                        # Point text first: Qdrant payloads carry it,
                        # but InMemory/SQLite points keep it on the
                        # point itself, and plan recall read only the
                        # payload, so those stores recalled empty.
                        text = str(getattr(point, "text", "") or
                                   payload.get("text", "")).strip()
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
        # Research-mode deep findings ride along with any targeted
        # research context, on every plan call in the run (fresh,
        # spec, repair, replan) — set once by run(), read here.
        deep_context = str(getattr(self, "_deep_research_context", "") or "")
        if deep_context:
            research_context = (deep_context + "\n" + research_context).strip() \
                if research_context else deep_context
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
        user_message = self._attachment_message(user)
        reply = self._chat(_phase, [Message("system", system), user_message])
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
            raw_skills = data.get("skills", []) or []
            if isinstance(raw_skills, str):
                raw_skills = [raw_skills]
            plan = Plan(steps=steps, deliverables=deliverables,
                        criteria=[str(c) for c in data.get("criteria", [])],
                        needs_research=[str(x) for x in data.get("needs_research", []) or []],
                        skills=[str(s) for s in raw_skills])
        except (json.JSONDecodeError, AttributeError):
            return Plan(
                steps=[Step(tool="write", args={"file": "deliverable.md",
                                                "text": f"Goal: {goal}\n"},
                            deliverable="deliverable.md")],
                deliverables=["deliverable.md"],
                criteria=["deliverable.md exists"])
        # Level 2, one expansion pass at most: the planner asked
        # for full skill texts by name. Unknown names are
        # ignored, never a failure; the expanded plan's own
        # list is never expanded again (skill_full is set).
        if skill_full is None and not self.kernel_mode \
                and self.plugins is not None and plan.skills:
            try:
                texts = self.plugins.skill_texts()
            except Exception:
                texts = {}
            resolved = {}
            for wanted in plan.skills:
                if wanted in texts and wanted not in resolved:
                    resolved[wanted] = texts[wanted]
                    continue
                bare = [k for k in texts
                        if k.rsplit("/", 1)[-1] == wanted]
                if len(bare) == 1 and bare[0] not in resolved:
                    resolved[bare[0]] = texts[bare[0]]
            if resolved:
                self._trace("plan", "skills-expanded", ", ".join(resolved))
                return self._plan(goal, failure_context=failure_context,
                                  spec=spec,
                                  research_context=research_context,
                                  skill_full=resolved)
        return plan

    def _attachment_message(self, text: str) -> Message:
        """Preserve the user text and attach WebUI images when present."""
        if not self.image_paths:
            return Message("user", text)
        from ..system.model_client import image_file_message
        return image_file_message(text, self.image_paths)

    def _research_and_replan(self, plan: Plan, goal: str,
                             spec=None) -> Plan:
        """Up to AIDON_MAX_REPLANS research + replan passes (default 1).

        Each pass researches the current plan's open questions
        (needs_research), then calls the planner once more with
        those notes as context, and the replanned plan replaces
        the original. At the default of 1 this is exactly the
        old single pass: a replanned plan's own needs_research
        is not researched again. Above 1, each further pass
        researches the replanned plan's new open questions.
        No researcher means no research: the original plan is
        kept. A failed or invalid replan keeps the plan it
        started that pass from and records the failure in
        _replan_note (copied onto the report by _finalize);
        it never fails the run.
        """
        max_replans = _resolve_count("AIDON_MAX_REPLANS", MAX_REPLANS, 0)
        current = plan
        for _ in range(max_replans):
            items = [str(x) for x in (getattr(current, "needs_research", None) or [])]
            if not items or self.researcher is None:
                return current
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
                return current
            if not isinstance(replanned, Plan) or (not replanned.steps
                                                   and not replanned.deliverables):
                self._replan_note = ("replan after research returned an invalid "
                                     "plan (no steps or deliverables), keeping "
                                     "the original plan")
                return current
            current = replanned
        return current

    def run(self, goal: str, spec=None, research_report=None,
            live_actions=None) -> RunReport:
        self._elapsed_timer = ElapsedTimer().start()
        # Time box starts here, covering research, planning, and
        # execution. A deadline consumed by planning or research
        # trips at the first execution step instead of cutting
        # those phases off mid-call.
        self._timeboxed = False
        try:
            deadline = float(getattr(self, "deadline_seconds", None) or 0)
        except (TypeError, ValueError):
            deadline = 0
        self._deadline_at = (time.monotonic() + deadline) if deadline > 0 else None
        # Creative requests get a wider response distribution. Keep this
        # narrow and phrase-based so ordinary tasks remain deterministic;
        # callers can still use AIDON_TEMP_* and AIDON_TOP_P_* overrides.
        self._creative_mode = bool(re.search(
            r"\b(creative writing|write (?:a|an|the) (?:story|poem|scene|script)|"
            r"short story|fiction|poem|poetry|screenplay|song lyrics)\b",
            str(goal), re.IGNORECASE))
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
        # Proceed of a Plan stop: the saved plan executes directly,
        # no routing, no fresh planning, no mode re-detection.
        if getattr(self, "resume_plan", None) is not None:
            return self._finalize(self._execute_plan(goal, self.resume_plan))
        if self.semantic_router is not None:
            try:
                try:
                    self.last_route = self.semantic_router.classify(
                        goal, judge=self._route_judge())
                except TypeError:
                    # Duck-typed routers with the old one-argument
                    # classify keep working; the TypeError fires at
                    # call binding, before any routing work runs.
                    self.last_route = self.semantic_router.classify(goal)
                self._trace("interpret", "done", f"semantic route {self.last_route[0]} {self.last_route[1]:.2f}")
                if callable(self.route_callback):
                    self.route_callback(self.last_route)
            except Exception:
                self.last_route = None
        # Mode detection rides the interpret pass: only when no mode
        # was chosen (command, chooser, sticky flag) and no spec was
        # frozen. A chosen mode always wins; detection never
        # overrides it, and a frozen spec always executes.
        if (getattr(self, "mode", None) is None and spec is None
                and self.semantic_router is not None):
            try:
                detected = self.semantic_router.mode_for(goal)
            except Exception:
                detected = None
            if detected in ("ask", "plan"):
                self.mode = detected
                self._trace("interpret", "done", f"mode detected: {detected}")
        # Ask mode: interview, then stop. The interview asks through
        # the chat callback (no clock, no card); the spec decision is
        # the Proceed button, not an approval card, so no gate is
        # passed and no spec approval is asked here.
        if getattr(self, "mode", None) == "ask" and spec is None:
            if getattr(self, "question_ask", None) is None:
                return self._finalize(RunReport(
                    "failed", goal, Plan(steps=[], deliverables=[], criteria=[]),
                    False, False,
                    error="ask mode needs a question channel; use /ask in chat, WebUI, or the CLI",
                    backend=self._backend_name()))
            try:
                from .interview import InterviewRunner
                interviewer = InterviewRunner(
                    self.client, self.home, memory=self.memory, gate=None,
                    project_id=self.project_id)
                result = interviewer.start_from_message(
                    goal, ask=self.question_ask, approve=None)
            except Exception as exc:
                return self._finalize(RunReport(
                    "failed", goal, Plan(steps=[], deliverables=[], criteria=[]),
                    False, False, error=f"interview failed: {exc}"[:500],
                    backend=self._backend_name()))
            from ..interface.mode_stops import save_mode_stop
            spec_dict = (result.spec.to_dict()
                         if hasattr(result.spec, "to_dict") else {"goal": goal})
            stop_id = save_mode_stop(
                self.home, mode="ask", goal=goal, spec=spec_dict,
                conversation_key=getattr(self, "conversation_key", ""),
                project_id=self.project_id)
            return self._finalize(RunReport(
                "stopped", goal, Plan(steps=[], deliverables=[], criteria=[]),
                False, False,
                mode_stop={"stop_id": stop_id, "mode": "ask", "goal": goal},
                backend=self._backend_name()))
        # Write-intent guard, from Aidon: the router is a hint, not a
        # safety boundary. A goal that asks for a write, a save, or a file
        # is never answered by a path with no write tools, even if the
        # router said chat, because that would silently miss the task.
        _write_intent = any(word in goal.lower() for word in ("write", "save", "create a file", "export", ".md", ".py", ".txt", ".csv"))
        # Classification is advisory.  If the request reaches the
        # executable pipeline (especially because it writes a file), expose
        # the effective route, not the discarded low-confidence chat guess.
        if (_write_intent and self.last_route and
                self.last_route[0] in ("chat", "webchat")):
            self.last_route = ("agentic", 1.0)
            self._trace("interpret", "done",
                        "agentic route selected because the request changes state")
            if callable(self.route_callback):
                self.route_callback(self.last_route)
        # Research mode: deep research first, before recall and
        # planning, so every plan call in the run carries the
        # findings. A chat/webchat route is forced to agentic:
        # the user asked for research, not a chat answer. Sticky
        # mode means exactly that for the whole conversation;
        # toggle it off with /research when chat is wanted again.
        # force_web still wins: it is the explicit override.
        self._deep_research_context = ""
        if self.research_mode and not self.force_web:
            if self.last_route and self.last_route[0] in ("chat", "webchat"):
                self.last_route = ("agentic", 1.0)
                self._trace("interpret", "done",
                            "agentic route selected because research mode is on")
                if callable(self.route_callback):
                    self.route_callback(self.last_route)
            if self.deep_researcher is not None:
                self._trace("research", "started", "deep research")
                try:
                    report = self.deep_researcher(
                        goal,
                        lambda messages: self._chat("research", messages))
                    report = report or {}
                    self._deep_research_context = str(report.get("summary", "") or "")
                    sources = report.get("citations", []) or []
                    self._trace("research", "done",
                                f"deep research: {len(sources)} source(s)")
                except Exception as exc:
                    self._trace("research", "failed", str(exc)[:100])
        if self.force_web and self.researcher is not None and spec is None and not _write_intent:
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client,
                                  on_token=self.stream_callback,
                                  context=self.conversation_context)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.use_semantic_router_skip and self.last_route and self.last_route[0] == "chat" and spec is None and not _write_intent:
            reply = self._chat("respond", [self._attachment_message(goal)])
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.use_semantic_router_skip and self.last_route and self.last_route[0] == "webchat" and spec is None and not _write_intent and self.researcher is not None:
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client,
                                  on_token=self.stream_callback,
                                  context=self.conversation_context)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.last_route is None:
            self.last_route = ("agentic", 1.0)
            if callable(self.route_callback):
                self.route_callback(self.last_route)
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
        failure_note = ""
        if self.memory is not None:
            try:
                from ..memory.experience import recall_experiences, matching_failure
                try:
                    recall_limit = max(0, int(os.environ.get(
                        "AIDON_EPISODE_RECALL_LIMIT", "1") or "1"))
                except ValueError:
                    recall_limit = 1
                experiences = recall_experiences(
                    self.memory, goal, limit=max(recall_limit, 5))
                failure = matching_failure(self.memory, goal)
                if failure is None:
                    # DAG composition first: when the goal spans
                    # distinct known workflows, sequencing them
                    # beats adapting any single one and silently
                    # dropping the rest.
                    composed = self._composed_plan(experiences, goal)
                    if composed is not None:
                        plan, source_goals = composed
                        if plan.deliverables:
                            self._adapted = True
                            self._adapted_from = " + ".join(source_goals)
                            return self._finalize(self._execute_plan(goal, plan))
                else:
                    # Failed-example learning: a previous run
                    # failed for a similar goal, so the adapt
                    # fast-path is skipped and the planner gets
                    # the failure as repair context instead of
                    # replaying the failed approach.
                    failure_note = (
                        f"A previous run for a similar goal failed: {failure.lesson} "
                        f"(failed tools: {', '.join(failure.failed_tools) or 'none'}; "
                        f"failed deliverables: {', '.join(failure.failed_deliverables) or 'none'}). "
                        f"Do not repeat that approach.")
                if failure is None and experiences and experiences[0].dag_nodes:
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
                            criteria=[], parallel_ok=True)
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
            plan = self._plan(goal, failure_context=failure_note)
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

    def _propose_join_step(self, goal, dags):
        """One model call proposing a final combining step, or None.

        The model sees the goal and the combined workflows'
        deliverables and returns at most one step (tool, args,
        deliverable) that combines or synthesizes them all, or
        no step when no combining step is needed. The step's
        dependencies are forced to every combined sink by
        combine_dags, never taken from the model, so the
        synthesis cannot run before its inputs exist. A
        failed or unparseable reply keeps the join-less union.
        """
        if self.client is None:
            return None
        try:
            import json as _json
            from ..system.model_client import Message
            workflows = [[n.deliverable for n in dag.topological_order()
                          if n.deliverable] for dag in dags]
            prompt = (
                f"Goal: {goal}\nParallel workflows and their deliverables: "
                f"{_json.dumps(workflows)}\nReturn JSON only, with key 'steps': "
                "a list with at most one final step that combines or "
                "synthesizes all the workflows' deliverables into one "
                "result, or an empty list when no combining step is "
                "needed. Each step is an object with tool, args, "
                "deliverable. Do not invent tools.")
            reply = self._chat("plan", [Message("system", "Return JSON only."),
                                        Message("user", prompt)])
            data = _json.loads(reply)
            steps = data.get("steps", []) if isinstance(data, dict) else []
            if not steps or not isinstance(steps[0], dict):
                return None
            first = steps[0]
            if not first.get("deliverable"):
                return None
            from .dag import DAGNode
            return DAGNode(
                id="join", tool=str(first.get("tool", "write")),
                args=dict(first.get("args", {}))
                if isinstance(first.get("args", {}), dict) else {},
                deliverable=str(first["deliverable"]))
        except Exception:
            return None

    def _composed_plan(self, experiences, goal):
        """Build a plan by combining distinct recalled workflows, or None.

        Trigger: at least two recalled experiences with DAG
        nodes, each matching the goal shape (>= the DAG
        similarity threshold, AIDON_DAG_SIMILARITY, default
        0.3) and pairwise distinct from each other
        (shape overlap < 0.5, the store-merge threshold, so
        near-duplicate workflows are not combined twice).
        Up to five recalled workflows are considered.

        The union is parallel, not sequential: combined
        workflows keep only their own dependencies, so
        independent checks run concurrently and only a step
        that declares a dependency waits. A sequential
        A-then-B composition stays available as compose_dags
        for callers that order workflows explicitly.

        Join: one model call at most proposes the final
        combining step (a synthesis node depending on every
        combined sink, such as a forecast from three
        independent checks). The model may return no step
        when no combining step is needed; a failed join
        keeps the join-less union.
        """
        from ..memory.experience import dag_similarity_threshold, goal_shape_for
        from .dag import combine_dags, dag_from_experience

        def overlap(a: str, b: str) -> float:
            set_a, set_b = set(a.split()), set(b.split())
            return len(set_a & set_b) / len(set_a | set_b) if (set_a | set_b) else 0.0

        goal_shape = goal_shape_for(goal)
        candidates = []
        for exp in experiences:
            if not exp.dag_nodes:
                continue
            exp_shape = exp.goal_shape or goal_shape_for(exp.goal)
            if overlap(exp_shape, goal_shape) < dag_similarity_threshold():
                continue
            if any(overlap(exp_shape, cand.goal_shape or goal_shape_for(cand.goal)) >= 0.5
                   for cand in candidates):
                continue
            candidates.append(exp)
            if len(candidates) >= 5:
                break
        if len(candidates) < 2:
            return None
        dags = [dag_from_experience(exp) for exp in candidates]
        combined = combine_dags(dags, join=self._propose_join_step(goal, dags))
        if combined is None:
            combined = combine_dags(dags)
        if combined is None:
            return None
        try:
            combined = combined.relativize(self.sandbox.workdir)
        except Exception:
            pass
        steps = combined.to_plan_steps()
        plan = Plan(
            steps=steps,
            deliverables=[str(s["deliverable"]) for s in steps if s.get("deliverable")],
            criteria=[], parallel_ok=True)
        plan = self._research_and_replan(plan, goal)
        if not plan.deliverables:
            return None
        return plan, [exp.goal for exp in candidates]

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
        # A direct chat/webchat answer is a successful completed exchange,
        # even though it has no file plan. Keep it in episodic memory so
        # ordinary conversation is learnable too; working memory contains
        # the verbatim turn, while this point is the durable summary hook.
        if report.status == "chat" and self.memory is not None:
            try:
                episodic = self.memory.record_episodic(
                    self.project_id, report.goal,
                    {"kind": "chat", "outcome": report.chat_reply or "",
                     "status": "done"})
                self.memory.promote_to_ltm(episodic, kind="semantic")
            except Exception:
                pass
        # Background fact extraction for this turn pair (user prompt +
        # assistant response): facts + understandings into episodic /
        # semantic only. Procedural learning below is separate and
        # untouched. Best effort, never fails the finished run.
        if report.status == "chat":
            try:
                from ..memory.extraction import spawn_extraction
                spawn_extraction(self.memory, self.project_id,
                                 f"User: {report.goal}\nAssistant: {report.chat_reply or ''}",
                                 self.client)
            except Exception:
                pass
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
            # Background fact extraction for the whole agentic run
            # (not per step): same mechanism as chat turns, once at
            # the end. Procedural learning above is separate.
            try:
                from ..memory.extraction import spawn_extraction
                deliverables = ", ".join(report.plan.deliverables or [])
                files = ", ".join(report.files or [])
                spawn_extraction(self.memory, self.project_id,
                                 f"Goal: {report.goal}\nDeliverables: {deliverables}\nFiles: {files}",
                                 self.client)
            except Exception:
                pass
        # Failed-example learning: a failed run is evidence
        # too. Record what failed (goal, error, tools, the
        # deliverables never produced) so a similar goal plans
        # fresh instead of replaying the failed approach.
        # Rejected and cancelled runs are not recorded: a
        # human rejection or an interruption is not evidence
        # that the approach was wrong. Best effort, as above.
        if report.status == "failed" and self.memory is not None:
            try:
                from ..memory.experience import record_failure
                produced = set(report.files or [])
                failed_deliverables = [d for d in report.plan.deliverables
                                       if d not in produced]
                failed_tools = []
                for step in report.plan.normalized_steps():
                    if step.tool not in failed_tools:
                        failed_tools.append(step.tool)
                record_failure(self.memory, report.goal,
                               (report.error or "")[:500],
                               failed_tools, failed_deliverables)
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

        if report.status in ("done", "failed", "rejected", "chat", "cancelled"):
            self._clear_checkpoint()
        reg = getattr(self, "_checkpoint_registry", None)
        if reg is not None:
            try:
                reg.close()
            except Exception:
                pass
            self._checkpoint_registry = None
        return report

    def _run_plan_step(self, step, goal, current_plan, passed, verified,
                       attempt, lock=None, save_checkpoint=True):
        """Execute one plan step in the sandbox run.

        Returns None on success, "cancelled" when cancellation
        was requested, or a failure message. Shared by the
        sequential loop and the parallel level driver, so both
        paths run the same step semantics: budget, llm-tier
        routing, deliverable binding, step lines, traces, and
        the passed set. In the parallel path, shared mutations
        go under the caller's lock, and per-step checkpoints
        are skipped — the level driver checkpoints once per
        level instead.
        """
        from contextlib import nullcontext
        guard = lock if lock is not None else nullcontext()
        if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
            return "cancelled"
        try:
            if self.budget is not None:
                self.budget.add_step()
            if not hasattr(self, "_step_lines"):
                self._step_lines = []
            from .step_lines import record_attempt
            with guard:
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
                tool_result = self._call_tool(step.tool, step.args)
                # File-producing extra tools return their actual
                # path. Bind that path to the step when the model
                # supplied a prose deliverable such as
                # "Aidon introduction PDF" instead of a filename.
                if step.tool in {"generate_document", "write_file"}:
                    artifact_path = getattr(tool_result, "path", "")
                    raw_result = str(tool_result).strip()
                    # Structured ToolResult is authoritative. Keep
                    # the prose fallback for older plugins/tools.
                    raw_path = artifact_path or (
                        raw_result[6:].strip()
                        if raw_result.lower().startswith("wrote ")
                        else raw_result)
                    candidate = Path(raw_path)
                    if not candidate.is_absolute():
                        candidate = self.sandbox.workdir / candidate
                    if candidate.is_file():
                        try:
                            actual = candidate.resolve().relative_to(
                                self.sandbox.workdir.resolve())
                            with guard:
                                old = step.deliverable
                                step.deliverable = str(actual)
                                current_plan.deliverables = [
                                    str(actual) if item == old else item
                                    for item in current_plan.deliverables]
                                # Criteria are model-authored too. Replace
                                # a prose filename with the path proven by
                                # the tool, so verification cannot ask for
                                # a non-existent label after a successful
                                # generation.
                                current_plan.criteria = [
                                    str(item).replace(str(old), str(actual))
                                    if old else item
                                    for item in current_plan.criteria]
                                if step.criteria and old:
                                    step.criteria = str(step.criteria).replace(
                                        str(old), str(actual))
                        except ValueError:
                            pass
            with guard:
                record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "done")
            self._trace("sandbox", "done", step.tool)
            if step.deliverable:
                with guard:
                    passed.add(step.deliverable)
            if save_checkpoint:
                self._save_checkpoint(
                    "sandbox", goal, current_plan, passed,
                    verified=verified, attempt=attempt)
            return None
        except Exception as exc:
            msg = f"step {step.tool} failed: {exc}"
            try:
                from .step_lines import record_attempt
                with guard:
                    record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "failed", str(exc)[:100])
            except Exception:
                pass
            self._trace("sandbox", "failed", str(exc)[:100])
            return msg

    def _run_plan_levels(self, goal, current_plan, passed, verified, attempt):
        """Execute a DAG-derived plan level by level, concurrently.

        Returns (failures, cancelled). Levels come from
        compute_levels over the steps' declared dependencies:
        a level runs only after every earlier level finished,
        so a step never runs before its dependencies, and
        steps in one level run together in a thread pool
        (workers from AIDON_PARALLEL_WORKERS, default 3, the
        same cap as swarm). A step whose dependency failed
        is skipped with a failure message, and the skip
        propagates to its dependents, so a failed check never
        feeds a synthesis step. Streaming is suppressed for
        the run of a level: parallel llm steps must not
        interleave deltas into one chat stream.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from .dag import compute_levels
        from .dag_executor import resolve_parallel_workers

        steps = current_plan.normalized_steps()
        by_id = {step.id: step for step in steps}
        try:
            levels = compute_levels([(step.id, list(step.depends_on or []))
                                     for step in steps])
        except ValueError as exc:
            return [f"parallel schedule failed: {exc}"], False
        workers = resolve_parallel_workers()
        lock = threading.Lock()
        failures: list[str] = []
        done_ids: set[str] = set()
        failed_ids: set[str] = set()
        skipped_ids: set[str] = set()
        cancelled = False
        saved_callback = self.stream_callback
        if saved_callback is not None:
            self.stream_callback = None
        try:
            for level in levels:
                if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                    cancelled = True
                    break
                if self._deadline_exceeded():
                    self._timeboxed = True
                    break
                runnable = []
                for step_id in level:
                    step = by_id[step_id]
                    if step.deliverable and step.deliverable in passed:
                        done_ids.add(step_id)
                        continue
                    blocked = [d for d in step.depends_on
                               if d in failed_ids or d in skipped_ids]
                    if blocked:
                        skipped_ids.add(step_id)
                        failures.append(
                            f"step {step.tool} skipped: dependency {blocked[0]} failed")
                        continue
                    runnable.append(step)
                if not runnable:
                    continue

                def _one(step):
                    return step.id, self._run_plan_step(
                        step, goal, current_plan, passed, verified, attempt,
                        lock=lock, save_checkpoint=False)

                with ThreadPoolExecutor(max_workers=min(workers, len(runnable))) as pool:
                    outcomes = list(pool.map(_one, runnable))
                for step_id, error in outcomes:
                    if error == "cancelled":
                        cancelled = True
                    elif error is not None:
                        failed_ids.add(step_id)
                        failures.append(error)
                    else:
                        done_ids.add(step_id)
                try:
                    self._save_checkpoint(
                        "sandbox", goal, current_plan, passed,
                        verified=verified, attempt=attempt)
                except Exception as exc:
                    failures.append(f"checkpoint failed: {exc}")
                if cancelled:
                    break
        finally:
            self.stream_callback = saved_callback
        return failures, cancelled

    def _deadline_exceeded(self) -> bool:
        """True when the run's wall-clock deadline has passed.

        Enforced between execution steps, never mid-step: a step
        already running (including a shell step, capped by the
        sandbox timeout) finishes first. Planning and research
        phases are not interrupted; a deadline consumed there
        trips at the first execution step instead.
        """
        deadline = getattr(self, "_deadline_at", None)
        return deadline is not None and time.monotonic() >= deadline

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
        # Skill names for grounding: plugin skills ("plugin/skill")
        # plus bare names, so a model writing either form in the
        # tool slot gets the skill-not-tool rejection, not a
        # generic unknown-tool failure.
        _skill_names: set[str] = set()
        if self.plugins is not None:
            try:
                _texts = self.plugins.skill_texts()
                _skill_names = set(_texts) | \
                    {name.rsplit("/", 1)[-1] for name in _texts}
            except Exception:
                pass
        plan_failures = validate_plan(plan, allowed_tools=_allowed,
                                      skill_names=_skill_names)
        if plan_failures:
            # A model can describe desired outputs while omitting executable
            # steps. Give it a bounded repair pass before rejecting the run.
            failure_text = "; ".join(plan_failures)
            repaired = None
            for _ in range(_resolve_count("AIDON_MAX_PLAN_REPAIRS", MAX_PLAN_REPAIRS, 0)):
                try:
                    candidate = self._plan(
                        goal,
                        failure_context=("The proposed plan was rejected by "
                                         "the deterministic validator: "
                                         f"{failure_text}. Return a complete "
                                         "JSON plan with executable steps; "
                                         "each deliverable must match a step."))
                    candidate_failures = validate_plan(candidate,
                                                       allowed_tools=_allowed,
                                                       skill_names=_skill_names)
                    if not candidate_failures:
                        repaired = candidate
                        break
                    plan = candidate
                    failure_text = "; ".join(candidate_failures)
                except Exception as exc:
                    failure_text = f"plan repair failed: {exc}"
                    break
            if repaired is None:
                return RunReport("failed", goal, plan, False, False,
                                 error=failure_text[:500])
            plan = repaired
        # Critic (reflection) pass, after deterministic validation
        # passes: one model judgment on coverage and ordering,
        # before any step runs. Issues feed the same bounded
        # repair path as validation failures. When rounds run
        # out, the latest plan proceeds with the issues traced
        # — the critic advises, never blocks.
        if _resolve_count("AIDON_CRITIC_ENABLED", 1, 0):
            from .critic import review_plan
            for _ in range(_resolve_count("AIDON_CRITIC_MAX_ROUNDS", 1, 0)):
                self._trace("critic", "started", "reviewing plan")
                try:
                    issues = review_plan(
                        plan, goal, _allowed, skill_names=_skill_names,
                        chat=lambda messages: self._chat("critic", messages))
                except Exception:
                    issues = []
                self._trace("critic", "done",
                            f"{len(issues)} issue(s)" if issues
                            else "plan clean")
                if not issues:
                    break
                try:
                    candidate = self._plan(
                        goal,
                        failure_context=("The plan critic found these "
                                         "gaps; fix them and return a "
                                         "complete JSON plan: " +
                                         "; ".join(issues)))
                    candidate_failures = validate_plan(
                        candidate, allowed_tools=_allowed,
                        skill_names=_skill_names)
                except Exception as exc:
                    self._trace("critic", "failed",
                                f"critic repair failed: {exc}"[:100])
                    break
                if candidate_failures:
                    # The critic's repair broke validation: keep the
                    # valid plan the critic reviewed, stop advising.
                    self._trace("critic", "failed",
                                "critic repair failed validation")
                    break
                plan = candidate
        # A document goal must have a document-producing step. A model can
        # occasionally substitute ``list`` and then claim that the listing
        # proves a PDF exists; that can never satisfy the user's request.
        # Use the registered deterministic document tool as a safe fallback
        # instead of allowing a known-invalid plan to reach verification.
        document_request = bool(re.search(
            r"\b(?:create|write|make|generate|produce|save|put)\b.*\b(?:pdf|document)\b|"
            r"\b(?:pdf|document)\b.*\b(?:create|write|make|generate|produce|save)\b",
            str(goal), re.IGNORECASE | re.DOTALL))
        has_document_step = any(
            str(step.tool).lower() in {"generate_document", "write_file"}
            for step in plan.normalized_steps())
        if document_request and not has_document_step:
            if "generate_document" not in self.extra_tools:
                return RunReport(
                    "failed", goal, plan, False, False,
                    error="document request cannot run: generate_document tool is unavailable")
            plan = Plan(
                steps=[Step(
                    tool="generate_document",
                    args={"title": "Aidon document", "content": goal,
                          "format": "pdf",
                          "filename": "generated_document.pdf"},
                    deliverable="generated_document.pdf",
                    criteria="PDF file is created inside workdir")],
                deliverables=["generated_document.pdf"],
                criteria=["PDF file is created inside workdir",
                          "Output message provides link to the PDF"])
            self._trace("plan", "repaired",
                        "replaced list-only document plan with generate_document")
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
        # Plan mode stops here: the plan passed validation, the
        # critic pass, the document fallback, and the cycle guard,
        # so the plan shown is the plan that would execute. No step
        # runs; the Proceed button resumes it via resume_plan.
        if getattr(self, "mode", None) == "plan":
            from .checkpoint import plan_to_dict
            from ..interface.mode_stops import save_mode_stop
            stop_id = save_mode_stop(
                self.home, mode="plan", goal=goal, plan=plan_to_dict(plan),
                conversation_key=getattr(self, "conversation_key", ""),
                project_id=self.project_id)
            return RunReport("stopped", goal, plan, False, False,
                             mode_stop={"stop_id": stop_id, "mode": "plan",
                                        "goal": goal,
                                        "deliverables": list(plan.deliverables)},
                             backend=self._backend_name())
        passed: set[str] = set()
        verified: list[str] = []
        resume = self._load_checkpoint(self.sandbox.workdir, goal=goal,
                                       plan=plan)
        if resume is not None and resume.phase in ("sandbox", "verified", "approved"):
            passed = set(resume.passed)
            verified = list(resume.verified)
            self._trace("checkpoint", "resumed",
                        f"phase={resume.phase} passed={len(passed)}")
        current_plan = plan
        max_attempts = _resolve_count("AIDON_MAX_ATTEMPTS", MAX_ATTEMPTS, 1)
        for attempt in range(1, max_attempts + 1):
            failures: list[str] = []
            # Cancellation, checked before every attempt, and steer,
            # applied to the next repair attempt, not to passed work.
            if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                return RunReport("cancelled", goal, current_plan, False, False,
                                 files=sorted(passed), attempts=attempt,
                                 backend=self._backend_name())
            # Execute steps, skipping steps whose deliverable
            # already passed in an earlier attempt (repair rule).
            if current_plan.parallel_ok:
                level_failures, level_cancelled = self._run_plan_levels(
                    goal, current_plan, passed, verified, attempt)
                if level_cancelled:
                    return RunReport("cancelled", goal, current_plan, False, False,
                                     files=sorted(passed), attempts=attempt,
                                     backend=self._backend_name())
                failures.extend(level_failures)
            else:
                for step in current_plan.normalized_steps():
                    if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                        return RunReport("cancelled", goal, current_plan, False, False,
                                         files=sorted(passed), attempts=attempt,
                                         backend=self._backend_name())
                    if step.deliverable and step.deliverable in passed:
                        continue
                    if self._deadline_exceeded():
                        self._timeboxed = True
                        break
                    error = self._run_plan_step(
                        step, goal, current_plan, passed, verified, attempt)
                    if error is not None:
                        failures.append(error)
            # Time box: stop here, before verification. Verifying
            # a partial plan would fail on the missing
            # deliverables and misreport a clean cut as a failure.
            # Partial is its own outcome: files kept, no failed
            # example recorded (a time cut is not evidence the
            # approach was wrong), no DAG learned from it.
            if getattr(self, "_timeboxed", False):
                self._trace("live", "done",
                            "time limit reached; partial results kept")
                return RunReport("partial", goal, current_plan, False, False,
                                 files=sorted(passed), attempts=attempt,
                                 error=(f"time limit reached; "
                                        f"{len(passed)} deliverable(s) kept"),
                                 backend=self._backend_name())
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
                    mv = self.model_verifier.verify(goal, current_plan.deliverables, self.sandbox.workdir, criteria)
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
            # A step that wrote its file is provisionally passed
            # above; a verification failure on that file must
            # take it back out, or the repair attempt would skip
            # the very step it has to fix.
            for name in list(passed):
                if any(name in msg for msg in failures):
                    passed.discard(name)
            for name in verified:
                if not any(name in msg for msg in failures):
                    passed.add(name)
            if model_failed:
                passed.clear()
            if ok and not model_failed:
                self._save_checkpoint(
                    "verified", goal, current_plan, passed,
                    verified=verified, attempt=attempt)
                break
            if attempt == max_attempts:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="; ".join(failures)[:500],
                                 attempts=attempt)
            # Repair: ask for a repaired plan, passed files untouched.
            # Raise effort only for substantive verification rejection. A
            # shell/tool/network exception is operational, not something
            # more reasoning can fix.
            if verify_failures or model_failed:
                self._effort_escalated = True
                if self.model_verifier is not None:
                    self.model_verifier.effort_escalated = True
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
            attempt = max_attempts
        files = sorted(passed) if passed else list(current_plan.deliverables)
        self._mission_prepare_review(goal, current_plan, files, sorted(verified))
        self._trace("approval", "started", "live approval requested")
        decision: Decision = self._ask_for_approval(
            "live", "Approve live execution?",
            self._approval_body(f"Goal: {goal}\nDeliverables: {', '.join(files)}"),
            force_ask=True,
            reversible=plan_is_reversible(
                current_plan, self.allowed_side_effect_tools),
        )
        # edited means "change the proposal" — not approve the original.
        # Live card has no apply-edit path; require a clean approved.
        approved = decision.verdict in ("approved", "auto_approved")
        self._mission_approval(approved)
        if not approved:
            return RunReport("rejected", goal, current_plan, True, False,
                             files=files, attempts=attempt,
                             backend=self._backend_name())
        self._save_checkpoint(
            "approved", goal, current_plan, passed,
            verified=verified, attempt=attempt)
        # Live execution...
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
                    # Store the full node, not just tool/args/
                    # deliverable: a recalled workflow must
                    # restore its real dependencies and tier,
                    # not a sequential chain invented at recall
                    # time. criteria rides as expected, the
                    # step's own success condition.
                    nodes.append({"id": step.id,
                                  "tool": step.tool, "args": step.args,
                                  "deliverable": step.deliverable,
                                  "depends_on": list(step.depends_on or []),
                                  "executor": step.executor,
                                  "expected": step.criteria})
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
                self.memory.promote_to_ltm(episodic, kind="semantic")
            except Exception:
                pass
        self._trace("learn", "done", "experience recorded")
        return RunReport("done", goal, current_plan, True, True,
                         files=files, attempts=attempt,
                         backend=self._backend_name())
