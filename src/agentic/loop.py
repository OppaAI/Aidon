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
Exception: a model-verifier NO that names no file keeps
every deliverable repairable, since no file is known-good.
At most 3 attempts in total (1 initial + 2 repairs),
then the run fails, it does not ask for approval, and
it does not go live.

Memory, blank by design: a new home starts with empty
collections, no import from previous chats. When a
memory is present, the loop recalls Experiences before
planning fresh, records episodic memory scoped by
conversation_id, and builds an Experience after a successful
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
from pathlib import Path

from ..interface.approval import ApprovalGate, Decision
from ..system.model_client import Message, ModelClient
from ..capabilities.sandbox import Sandbox
from ..capabilities.tools import ToolRegistry
from .verify import verify_plan
from .capability_interview import CapabilityInterviewMixin
from .plan_execution import PlanExecutionMixin
from .plan_run import PlanRunMixin
from .tool_dispatch import ToolDispatchMixin, _normalize_shell_args
from .run_finalization import RunFinalizationMixin
from .run_support import RunSupportMixin
from .planning import PlanningMixin, MAX_REPLANS, _SKILL_FULL_CHARS
from .elapsed import ElapsedTimer

from .run_types import (MAX_ATTEMPTS, MAX_PLAN_REPAIRS, Plan, RunReport, SIDE_EFFECT_TOOL_WORDS, Step,
                        _critic_wanted, _mentions_file, _model_verifier_wanted,
                        _resolve_count, _resolve_critic_mode,
                        _resolve_model_verifier_mode, format_duration,
                        format_elapsed, format_run_stats, plan_is_reversible)


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




class GoalRunner(RunSupportMixin, ToolDispatchMixin, PlanExecutionMixin, PlanRunMixin, PlanningMixin, RunFinalizationMixin, CapabilityInterviewMixin):
    def __init__(self, client: ModelClient, sandbox: Sandbox,
                 gate: ApprovalGate, home: Path, memory=None,
                 conversation_id: str = "",
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
                 conversation_key: str = "", resume_plan=None,
                 clef_client=None, acquisition_governor=None, resume_interview=False,
                 conversation_thread_id: str = ""):
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
        self.conversation_id = conversation_id
        self.conversation_thread_id = str(conversation_thread_id or "")
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
        # Clef decision client (None = Clef not configured): used by
        # capability triage in run(). Routing via Clef arrives as
        # the semantic_router argument instead (ClefRouter).
        self.clef_client = clef_client
        self.acquisition_governor = acquisition_governor
        self.resume_interview = bool(resume_interview)
        self.kernel_mode = kernel_mode
        self.semantic_router = semantic_router
        self.use_semantic_router_skip = use_semantic_router_skip
        self._adapted = False
        self._adapted_from = ""
        self._blocked: list[str] = []
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
        self._image_context_isolated = False
        self.route_callback = route_callback
        self.trace_callback = trace_callback

    def _chat(self, phase: str, messages, usage_phase: str | None = None) -> str:
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
                    return chat_phase(phase, messages, sampling=sampling,
                                      usage_phase=usage_phase)
                except TypeError:
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
                return self.client.chat_phase(
                    phase, messages, sampling=sampling,
                    usage_phase=usage_phase)
            except TypeError:
                try:
                    return self.client.chat_phase(phase, messages,
                                                  sampling=sampling)
                except TypeError:
                    return self.client.chat_phase(phase, messages)
        try:
            return self.client.chat(messages, sampling=sampling)
        except TypeError:
            return self.client.chat(messages)

    def _chat_goal_with_memory(self, goal: str) -> str:
        """Add binding guidance and relevant memory to plain chat."""
        parts = [goal]
        if str(getattr(self, "guidance", "") or "").strip():
            try:
                from .prompts import guidance_block
                parts.append(guidance_block(self.guidance))
            except Exception:
                parts.append(str(self.guidance).strip())
        try:
            context = self._memory_context(goal)
        except Exception:
            context = ""
        if context:
            parts.append(
                "When a recalled fact directly answers the question, state "
                "the fact plainly. Do not reinterpret a word from the question "
                "as a competing answer or narrate the retrieval.")
            parts.append(context)
        return "\n\n".join(parts)

    def _backend_name(self) -> str:
        if self.execution_backend is not None:
            return self.execution_backend.name
        return "bubblewrap"



    def run(self, goal: str, spec=None, research_report=None,
            live_actions=None) -> RunReport:
        self._elapsed_timer = ElapsedTimer().start()
        # An attached image is the subject of this request. Keep unrelated
        # old chat turns and recalled episodes out unless the user explicitly
        # asks to connect the image to earlier conversation.
        from .context_policy import explicitly_refers_to_prior_context
        self._image_context_isolated = (bool(self.image_paths)
                                        and not explicitly_refers_to_prior_context(goal))
        if self._image_context_isolated:
            self.conversation_context = ""
        if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
            return self._finalize(RunReport(
                "cancelled", goal, Plan(), False, False,
                backend=self._backend_name()))
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
        # Computed early: the pre-interview intent skip below
        # needs it, and the write-intent guard below reuses it.
        _write_intent = any(word in goal.lower() for word in ("write", "save", "create a file", "export", ".md", ".py", ".txt", ".csv"))
        # Intent routing applies in every mode, before any
        # mode interview: a chat-routed message gets a plain
        # reply and a webchat-routed message a research-backed
        # reply, with no interview and no spec. Research mode
        # and force_web keep their existing paths below, so
        # they are excluded here.
        _route = getattr(self, "last_route", None)
        if (not getattr(self, "resume_interview", False) and getattr(self, "use_semantic_router_skip", False) and _route
                and _route[0] == "chat" and spec is None and not _write_intent
                and not getattr(self, "research_mode", False)
                and not getattr(self, "force_web", False)):
            reply = self._chat("respond", [self._attachment_message(
                self._chat_goal_with_memory(goal))])
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if (not getattr(self, "resume_interview", False) and getattr(self, "use_semantic_router_skip", False) and _route
                and _route[0] == "webchat" and spec is None and not _write_intent
                and not self.image_paths
                and self.researcher is not None
                and not getattr(self, "research_mode", False)
                and not getattr(self, "force_web", False)):
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client,
                                  on_token=self.stream_callback,
                                  context=self.conversation_context,
                                  guidance=self.guidance)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
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
                    conversation_id=self.conversation_id)
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
                conversation_id=self.conversation_id,
                thread_ts=getattr(self, "conversation_thread_id", ""),
                budget_key=getattr(
                    getattr(self, "acquisition_governor", None),
                    "budget_key", None) or "")
            return self._finalize(RunReport(
                "stopped", goal, Plan(steps=[], deliverables=[], criteria=[]),
                False, False,
                mode_stop={"stop_id": stop_id, "mode": "ask", "goal": goal},
                backend=self._backend_name()))
        # Write-intent guard, from Aidon: the router is a hint, not a
        # safety boundary. A goal that asks for a write, a save, or a file
        # is never answered by a path with no write tools, even if the
        # router said chat, because that would silently miss the task.
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
        if (not getattr(self, "resume_interview", False) and self.force_web
                and self.researcher is not None and spec is None
                and not _write_intent and not self.image_paths):
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client,
                                  on_token=self.stream_callback,
                                  context=self.conversation_context,
                                  guidance=self.guidance)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if not getattr(self, "resume_interview", False) and self.use_semantic_router_skip and self.last_route and self.last_route[0] == "chat" and spec is None and not _write_intent:
            reply = self._chat("respond", [self._attachment_message(
                self._chat_goal_with_memory(goal))])
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if (not getattr(self, "resume_interview", False)
                and self.use_semantic_router_skip and self.last_route
                and self.last_route[0] == "webchat" and spec is None
                and not _write_intent and self.researcher is not None
                and not self.image_paths):
            from ..capabilities.webchat import webchat_reply
            reply = webchat_reply(goal, self.researcher, self.client,
                                  on_token=self.stream_callback,
                                  context=self.conversation_context,
                                  guidance=self.guidance)
            report = RunReport("chat", goal, Plan(steps=[], deliverables=[], criteria=[]),
                               False, False, chat_reply=reply)
            return self._finalize(report)
        if self.last_route is None:
            self.last_route = ("agentic", 1.0)
            if callable(self.route_callback):
                self.route_callback(self.last_route)
        self._start_mission(goal)
        # Capability triage, before any planning: inventory what
        # Aidon has, classify the gap with Clef when configured,
        # and stop with a notification when blocked instead of
        # planning an impossible goal. Skipped silently when no
        # Clef client is configured or triage itself fails — an
        # uncertain triage must never block a doable goal. Runs
        # resumed from a Plan stop skip triage (they return
        # before this point): a plan the user already approved
        # is not re-blocked unless a tool disappeared, which the
        # execution-time lookup still catches.
        self._triage_note = ""
        if (not getattr(self, "approved_capability_brief", None)
                and (getattr(self, "clef_client", None) is not None
                     or getattr(self, "question_ask", None) is not None)):
            try:
                from .capability_triage import build_inventory, triage, inventory_text
                _inventory = build_inventory(
                    self.home, self.tools, self.plugins, self.extra_tools)
                _spec_text = ""
                if spec is not None:
                    try:
                        _spec_text = json.dumps(
                            spec.to_dict() if hasattr(spec, "to_dict") else spec)[:1000]
                    except Exception:
                        _spec_text = ""
                from ..interface.interview_store import InterviewStore
                _saved = InterviewStore(self.home).unfinished_for_conversation(getattr(self, "conversation_id", ""), goal=goal)
                _clef = getattr(self, "clef_client", None)
                from .capability_triage import builtin_image_edit_covers
                if builtin_image_edit_covers(goal, self.extra_tools):
                    # A saved Lab interview must not outrank a tool this run
                    # can already call.
                    _triage = {"status": "covered", "confidence": 1.0,
                               "note": "Built-in edit_image covers this attached-image edit."}
                elif _saved and _saved.get("goal") == goal and _saved.get("kind") in ("need_plugin", "need_skill", "need_mcp"):
                    _triage = {"status": _saved["kind"], "confidence": 1.0, "note": "Resume saved interview"}
                elif _clef is not None:
                    _triage = triage(goal, _inventory, _clef, spec_text=_spec_text)
                else:
                    from .planner import _extract_json
                    _reply = self._chat("interpret", [Message("user", (
                        "Assess the goal against this actual capability inventory. "
                        "A tool performs an action, a skill is a reusable procedure, "
                        "and MCP connects a service. Do not assume an unlisted capability exists. "
                        "Return JSON only: {\"status\": \"covered|need_plugin|need_skill|need_mcp|blocked\"}.\n"
                        f"Goal: {goal}\n{inventory_text(_inventory)}"))])
                    _status = _extract_json(_reply).get("status")
                    if _status not in ("covered", "need_plugin", "need_skill", "need_mcp", "blocked"):
                        raise ValueError("capability assessment returned an invalid status")
                    _triage = {"status": _status, "confidence": 1.0,
                               "note": "A required capability could not be provided."}
                self._trace("capability", "triage",
                            f"{_triage['status']} ({_triage['confidence']:.2f})")
                if _triage["status"] == "blocked":
                    return self._finalize(RunReport(
                        "failed", goal, Plan(steps=[], deliverables=[], criteria=[]),
                        False, False, error=_triage["note"][:500],
                        backend=self._backend_name()))
                if _triage["status"] == "need_skill":
                    # A reusable procedure is an optimization learned from
                    # demonstrated success, not a prerequisite capability.
                    # Continue with the tools that actually exist.
                    self._triage_note = (
                        "No trusted skill currently covers this workflow. "
                        "Proceed using available tools. Only offer to distill "
                        "a skill after successful, verified completion.")
                elif _triage["status"] != "covered":
                    if getattr(self, "question_ask", None) is None:
                        self._triage_note = _triage["note"]
                    else:
                        _outcome, _brief = self._capability_interview(goal, _triage["status"])
                        if _outcome != "approved":
                            return self._finalize(RunReport(
                                "rejected" if _outcome == "rejected" else "failed",
                                goal, Plan(), False, False, error=_brief,
                                backend=self._backend_name()))
                        self._triage_note = ("The user approved this capability brief. "
                            "Use the existing capability creation or connection workflow, "
                            "test it, obtain its separate approvals, then fulfill the original goal.\n" + _brief)
            except Exception as exc:
                self._trace("capability", "triage-skipped", str(exc)[:100])
                if getattr(self, "question_ask", None) is not None:
                    return self._finalize(RunReport(
                        "failed", goal, Plan(), False, False,
                        error="I couldn’t check the capabilities needed for this goal. Please try again.",
                        backend=self._backend_name()))
        # A prior failed image workflow can be just as misleading as a
        # recalled episode; don't leak it into a standalone image request.
        _lessons_note = ("" if self._image_context_isolated
                         else self._failure_lessons_note(goal))
        if _lessons_note:
            self._triage_note = ((self._triage_note + "\n") if self._triage_note else "") + _lessons_note
            self._trace("capability", "failure-lessons", "recalled past failure lessons")
        if spec is not None:
            spec_deliverables = spec.deliverables if hasattr(spec, "deliverables") else spec.get("deliverables", [])
            try:
                plan = self._plan(goal, spec=spec, triage_note=self._triage_note)
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
        if self.memory is not None and not self._image_context_isolated:
            try:
                from ..memory.experience import recall_experiences, matching_failure
                try:
                    recall_limit = max(0, int(os.environ.get(
                        "AIDON_EPISODE_RECALL_LIMIT", "1") or "1"))
                except ValueError:
                    recall_limit = 1
                # Composition floor, fixed at 5: the adaptation path
                # always considers at least five past experiences, even
                # when AIDON_EPISODE_RECALL_LIMIT is set lower. The
                # floor cannot go lower; the setting only raises the
                # count above it. The plan-prompt block honors the
                # setting as written.
                experiences = recall_experiences(
                    self.memory, goal, limit=max(recall_limit, 5))
                # Clef DAG matching: re-rank the recalled workflows
                # by fit before adaptation picks one. Jaccard recall
                # stays the pre-filter; Clef only reorders within
                # the recalled set, and any failure or low
                # confidence keeps the recalled order.
                if getattr(self, "clef_client", None) is not None and experiences:
                    try:
                        from .clef_dag import rank_dags
                        _ranked = rank_dags(goal, experiences, getattr(self, "clef_client", None))
                        self._trace("dag", "ranked",
                                    f"{len(_ranked)} candidate(s) after Clef ranking")
                        experiences = _ranked
                    except Exception as exc:
                        self._trace("dag", "rank-skipped", str(exc)[:100])
                failure = matching_failure(self.memory, goal)
                _skip_adapt = bool(self.image_paths) and not self._is_image_edit_goal(goal)
                if failure is None and not _skip_adapt:
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
                if (failure is None and experiences and experiences[0].dag_nodes
                        and not _skip_adapt):
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
            plan = self._plan(goal, failure_context=failure_note,
                              triage_note=self._triage_note)
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

    def _is_image_edit_goal(self, goal: str) -> bool:
        low = (goal or "").lower()
        return any(w in low for w in (
            "edit", "modify", "transform", "retouch", "adjust", "enhance",
            "filter", "crop", "resize", "recolor", "remove background",
            "background removal", "inpaint", "outpaint", "change the scene",
            "change scene", "change the background", "change background",
            "replace the background", "replace background"))

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
