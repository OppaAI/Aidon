"""Plan execution, repair, and validation methods for GoalRunner."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from ..interface.approval import Decision
from ..system.model_client import Message
from .verify import verify_plan
from .run_types import (
    MAX_ATTEMPTS,
    MAX_PLAN_REPAIRS,
    Plan,
    RunReport,
    Step,
    _critic_wanted,
    _mentions_file,
    _model_verifier_wanted,
    _resolve_count,
    _resolve_model_verifier_mode,
    plan_is_reversible,
)


class PlanExecutionMixin:
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
        llm_step = step.tool == "llm"
        model_phase = "respond" if step.executor == "fast" else "plan"
        usage_phase = (f"llm-step:{step.id or step.deliverable or 'unnamed'}"
                       if llm_step else "sandbox")
        trace_key = f"{usage_phase}:{id(step)}" if llm_step else "sandbox"
        description = str(getattr(step, "description", "") or "").strip()
        if step.tool == "shell":
            import shlex
            raw_command = step.args.get("command", step.args.get("cmd", ""))
            command_text = (shlex.join([str(part) for part in raw_command])
                            if isinstance(raw_command, (list, tuple))
                            else str(raw_command))
            try:
                from ..memory.redaction import redact_text
                command_text = redact_text(command_text)
            except Exception:
                pass
            trace_detail = "Shell command: " + command_text[:1200]
        else:
            trace_detail = description or (
                f"{step.tool} → {step.deliverable}" if step.deliverable
                else f"Run tool: {step.tool}")
            if llm_step:
                trace_detail = "LLM step: " + trace_detail
        if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
            return "cancelled"
        try:
            skill_trusted = False
            if self.budget is not None:
                self.budget.add_step()
            if not hasattr(self, "_step_lines"):
                self._step_lines = []
            from .step_lines import record_attempt
            with guard:
                record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "started")
            trace_kind = "llm" if llm_step else "sandbox"
            self._trace(trace_kind, "started", trace_detail,
                        usage_phase=usage_phase, trace_key=trace_key)
            if step.tool == "write" and not step.args:
                step.args = {"file": step.deliverable,
                             "text": f"Goal: {goal}\n"}
            if step.tool == "llm":
                # DAG tier routing: an llm node runs on the
                # tier it declares — fast via the respond
                # phase, strong via the plan phase — and its
                # reply becomes the deliverable file.
                _prompt = str(step.args.get("prompt", "") or step.args.get("text", ""))
                _reply = self._chat(model_phase, [Message("user", _prompt)],
                                    usage_phase=usage_phase)
                if step.deliverable:
                    self._call_tool("write", {"file": step.deliverable, "text": _reply})
            else:
                tool_result = self._call_tool(step.tool, step.args)
                skill_trusted = (
                    step.tool == "adopt_skill"
                    and str(tool_result).startswith("Trusted skill "))
                if (step.tool == "self_modify"
                        and str(tool_result).startswith((
                            "self-modification refused:",
                            "self-modification aborted:",
                            "self-modification failed ("))):
                    raise RuntimeError(str(tool_result))
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
            self._trace(trace_kind, "done", "Completed: " + trace_detail,
                        usage_phase=usage_phase, trace_key=trace_key)
            if step.deliverable:
                with guard:
                    passed.add(step.deliverable)
            if save_checkpoint:
                self._save_checkpoint(
                    "sandbox", goal, current_plan, passed,
                    verified=verified, attempt=attempt)
            return "replan_skill" if skill_trusted else None
        except Exception as exc:
            msg = f"step {step.tool} failed: {exc}"
            try:
                from .step_lines import record_attempt
                with guard:
                    record_attempt(self._step_lines, step.deliverable or step.tool, step.tool, "failed", str(exc)[:100])
            except Exception:
                pass
            self._trace("llm" if llm_step else "sandbox", "failed",
                        (trace_detail + " · " + str(exc))[:500],
                        usage_phase=usage_phase, trace_key=trace_key)
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

    def _repair_criteria(self, goal: str, criteria: list[str],
                         failures: list[str]) -> list[str] | None:
        """Rewrite unverifiable criteria into checkable forms.

        Returns the full replacement criteria list, or None when
        the repair cannot be trusted (model error, unparsable
        reply, empty list). The caller re-verifies the SAME
        workdir against the new list without re-executing
        anything: files are untouched, only the assertions
        change. One call per run, guarded by the caller.
        """
        failed = [f[len("unverified criterion: "):] for f in failures]
        try:
            files = sorted(p.name for p in self.sandbox.workdir.iterdir()
                           if p.is_file())
        except Exception:
            files = []
        prompt = (
            f"Goal: {goal}\n"
            f"Files in the workdir: {', '.join(files) or '(none)'}\n"
            "These criteria failed verification because they are not "
            "in a checkable form:\n"
            + "\n".join(f"- {c}" for c in failed)
            + "\nAll criteria (keep the ones that already verify, "
            "rewrite the failed ones):\n"
            + "\n".join(f"- {c}" for c in criteria)
            + "\nRewrite the failed criteria into checkable forms only: "
            "'NAME exists', 'NAME exists in the workspace folder', "
            "'NAME is non-empty', 'NAME contains TEXT', 'NAME does not "
            "contain TEXT', 'ledger NAME "
            "total is CENTS'. One assertion each, no parenthetical "
            "hedges. Return JSON only: {\"criteria\": [...]}")
        try:
            reply = self._chat("plan", [Message("user", prompt)])
        except Exception:
            return None
        try:
            data = json.loads(reply)
        except Exception:
            match = re.search(r"\{.*\}", reply, re.DOTALL)
            if match is None:
                return None
            try:
                data = json.loads(match.group(0))
            except Exception:
                return None
        items = data.get("criteria") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return None
        cleaned = [str(c).strip() for c in items if str(c).strip()]
        return cleaned or None

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
        # A plan may author and adopt a plugin before using one of its
        # tools. Permit otherwise-unknown tool names only when the same
        # DAG contains an adoption step and the call depends on that
        # step. Dispatch still requires the live adopted registry, so a
        # rejected adoption cannot execute the draft.
        try:
            _steps_for_acquisition = plan.normalized_steps()
            _adoptions = {s.id for s in _steps_for_acquisition
                          if s.tool == "adopt_tool_plugin"}
            _by_id = {s.id: s for s in _steps_for_acquisition}

            _step_order = {s.id: i for i, s in
                           enumerate(_steps_for_acquisition)}

            def _adoption_ancestors(step_id, seen=None):
                seen = set() if seen is None else seen
                if step_id in seen:
                    return set()
                seen.add(step_id)
                found = set()
                for dependency in _by_id[step_id].depends_on:
                    if dependency in _adoptions:
                        found.add(dependency)
                    if dependency in _by_id:
                        found.update(_adoption_ancestors(dependency, seen))
                return found

            for candidate in _steps_for_acquisition:
                if (candidate.tool not in _allowed
                        and _adoptions
                        and any(_step_order.get(adopt_id, -1)
                                < _step_order.get(candidate.id, -1)
                                for adopt_id in _adoption_ancestors(
                                    candidate.id))):
                    _allowed.add(candidate.tool)
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
        # — the critic advises, never blocks. In risk mode the
        # pass is skipped for adapted or small plans with no
        # live steps, and the skip is traced so the saving is
        # visible: flip AIDON_CRITIC_MODE to "always" to restore
        # a critic pass on every plan.
        _critic_enabled = bool(_resolve_count("AIDON_CRITIC_ENABLED", 1, 0))
        _run_critic = _critic_enabled and _critic_wanted(plan, self._adapted_from)
        if _critic_enabled and not _run_critic:
            self._trace("critic", "skipped",
                        "risk-based: adapted or small plan, no live steps")
        if _run_critic:
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
                conversation_id=self.conversation_id,
                budget_key=getattr(
                    getattr(self, "acquisition_governor", None),
                    "budget_key", None) or "")
            return RunReport("stopped", goal, plan, False, False,
                             mode_stop={"stop_id": stop_id, "mode": "plan",
                                        "goal": goal,
                                        "deliverables": list(plan.deliverables)},
                             backend=self._backend_name())
        passed: set[str] = set()
        verified: list[str] = []
        # One criterion repair per run: when verification fails only
        # because criteria are unverifiable, rewriting the criteria
        # and re-verifying the untouched files beats re-executing
        # the plan and minting another attempt's files.
        self._criterion_repaired = False
        resume = self._load_checkpoint(self.sandbox.workdir, goal=goal,
                                       plan=plan)
        if resume is not None and resume.phase in ("sandbox", "verified", "approved"):
            passed = set(resume.passed)
            verified = list(resume.verified)
            self._trace("checkpoint", "resumed",
                        f"phase={resume.phase} passed={len(passed)}")
        current_plan = plan
        if (current_plan.parallel_ok
                and any(step.tool == "adopt_skill"
                        for step in current_plan.normalized_steps())):
            # Skill adoption changes planner context; it is a sequencing
            # boundary and must never run alongside steps based on the old
            # capability inventory.
            current_plan.parallel_ok = False
            self._trace("plan", "serialized",
                        "skill adoption is a replanning boundary")
        max_attempts = _resolve_count("AIDON_MAX_ATTEMPTS", MAX_ATTEMPTS, 1)
        for attempt in range(1, max_attempts + 1):
            failures: list[str] = []
            if attempt > 1:
                # A repair cycle under the acquisition governor:
                # the attempts cap stays authoritative, but the
                # governor bounds repairs even if attempts are
                # raised above it. Exhaustion fails the run with
                # the governor's message, not another repair.
                _gov = getattr(self, "acquisition_governor", None)
                if _gov is not None:
                    try:
                        _gov.acquire("repair")
                    except Exception as exc:
                        return RunReport("failed", goal, current_plan,
                                         False, False, error=str(exc)[:500],
                                         attempts=attempt,
                                         backend=self._backend_name())
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
                skill_replan_used = False
                while True:
                    completed_steps = []
                    replan_after_skill = False
                    steps = current_plan.normalized_steps()
                    # Skill trust can change the planner's available procedure
                    # context. Keep this path sequential so the next step cannot
                    # race ahead using a stale plan.
                    for step in steps:
                        if self.run_control is not None and self.run_control.is_cancelled(self.run_id):
                            return RunReport("cancelled", goal, current_plan, False, False,
                                             files=sorted(passed), attempts=attempt,
                                             backend=self._backend_name())
                        if step.deliverable and step.deliverable in passed:
                            completed_steps.append(step)
                            continue
                        if self._deadline_exceeded():
                            self._timeboxed = True
                            break
                        error = self._run_plan_step(
                            step, goal, current_plan, passed, verified, attempt)
                        if error == "replan_skill":
                            if (failures or skill_replan_used
                                    or not plan_is_reversible(Plan(steps=completed_steps))):
                                return RunReport(
                                    "failed", goal, current_plan, False, False,
                                    files=sorted(passed),
                                    error=("trusted skill is available, but the original "
                                           "plan cannot be safely resumed after failed, "
                                           "irreversible, or repeated capability-acquisition "
                                           "work"),
                                    attempts=attempt, backend=self._backend_name())
                            try:
                                current_plan = self._plan(
                                    goal,
                                    failure_context=(
                                        "A skill was just tested and separately trusted. "
                                        "Replan the original goal using its tested procedure "
                                        "where useful. Preserve completed draft files and "
                                        "do not repeat capability acquisition."),
                                    spec=getattr(self, "_current_spec", None),
                                    research_context=getattr(
                                        self, "_deep_research_context", ""),
                                    triage_note=getattr(self, "_triage_note", ""))
                                _allowed = set(ALLOWED_TOOLS) | {"llm"}
                                _allowed.update(self.extra_tools or {})
                                if self.plugins is not None:
                                    _allowed.update(self.plugins.tool_names())
                                    _texts = self.plugins.skill_texts()
                                    _skill_names = set(_texts) | {
                                        name.rsplit("/", 1)[-1] for name in _texts}
                                plan_failures = validate_plan(
                                    current_plan, allowed_tools=_allowed,
                                    skill_names=_skill_names)
                                if plan_failures or not current_plan.deliverables:
                                    raise ValueError("; ".join(plan_failures) or
                                                     "replanned goal has no deliverables")
                            except Exception as exc:
                                return RunReport(
                                    "failed", goal, current_plan, False, False,
                                    files=sorted(passed),
                                    error=f"skill-triggered replan failed: {exc}"[:500],
                                    attempts=attempt, backend=self._backend_name())
                            skill_replan_used = True
                            replan_after_skill = True
                            self._trace("plan", "replanned",
                                        "trusted skill added to original-goal plan")
                            self._save_checkpoint(
                                "sandbox", goal, current_plan, passed,
                                verified=verified, attempt=attempt)
                            if getattr(self, "mode", None) == "plan":
                                from .checkpoint import plan_to_dict
                                from ..interface.mode_stops import save_mode_stop
                                stop_id = save_mode_stop(
                                    self.home, mode="plan", goal=goal,
                                    plan=plan_to_dict(current_plan),
                                    conversation_key=getattr(self, "conversation_key", ""),
                                    conversation_id=self.conversation_id,
                                    budget_key=getattr(
                                        getattr(self, "acquisition_governor", None),
                                        "budget_key", None) or "")
                                return RunReport(
                                    "stopped", goal, current_plan, False, False,
                                    files=sorted(passed),
                                    mode_stop={"stop_id": stop_id, "mode": "plan",
                                               "goal": goal,
                                               "deliverables": list(current_plan.deliverables)},
                                    attempts=attempt, backend=self._backend_name())
                            break
                        if error is not None:
                            failures.append(error)
                        else:
                            completed_steps.append(step)
                    if replan_after_skill:
                        continue
                    break
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
            criteria_note = "; ".join(str(item) for item in criteria)
            checked_names = ", ".join(str(item)
                                       for item in current_plan.deliverables)
            verify_detail = ("Code verifier: checked that deliverables exist "
                             "and are non-empty; criteria: " +
                             (criteria_note or "none specified") +
                             "; files: " + (checked_names or "none"))
            try:
                from ..memory.redaction import redact_text
                verify_detail = redact_text(verify_detail)
            except Exception:
                pass
            self._trace("verify", "started", verify_detail[:1200])
            ok, verify_failures, verified = verify_plan(
                self.sandbox.workdir, current_plan.deliverables, criteria)
            outcome = ("Code verifier passed" if ok else "Code verifier failed")
            outcome += f"; verified {len(verified)} file(s)"
            if verify_failures:
                outcome += "; findings: " + "; ".join(verify_failures)
            try:
                from ..memory.redaction import redact_text
                outcome = redact_text(outcome)
            except Exception:
                pass
            self._trace("verify", "done" if ok else "failed", outcome[:1200])
            failures.extend(verify_failures)
            # Model verifier, second opinion only, after code
            # verification passes. A "no" triggers incremental repair,
            # an "unknown" reply does not override code. The mode
            # is tunable (AIDON_MODEL_VERIFIER off/selective/
            # always, default off); a skipped verifier is traced
            # so the saving is visible.
            model_failed = False
            verifier_mode = _resolve_model_verifier_mode()
            verifier_wanted = _model_verifier_wanted(
                verifier_mode, current_plan.deliverables)
            # Clef validate gate, after code verification passes
            # and before the model verifier: accept skips the
            # model call, a high-confidence retry acts as a
            # generic model NO (every deliverable stays
            # repairable), and anything else defers to the model
            # verifier as today. A gate failure also defers;
            # the gate never blocks a code-passing run.
            gate_skip_verifier = False
            if ok and getattr(self, "clef_client", None) is not None:
                try:
                    from .clef_validate import validate_gate
                    gate = validate_gate(
                        goal, current_plan.deliverables, criteria,
                        self.sandbox.workdir, getattr(self, "clef_client", None))
                    self._trace("verify", f"gate-{gate['action']}",
                                f"clef gate {gate['action']} "
                                f"({gate['confidence']:.2f})")
                    if gate["action"] == "accept":
                        gate_skip_verifier = True
                    elif gate["action"] == "retry":
                        model_failed = True
                        failures.append(f"clef gate: {gate['reason']}")
                except Exception as exc:
                    self._trace("verify", "gate-skipped", str(exc)[:100])
            if (ok and not model_failed and self.model_verifier is not None
                    and verifier_wanted and not gate_skip_verifier):
                try:
                    mv = self.model_verifier.verify(goal, current_plan.deliverables, self.sandbox.workdir, criteria)
                    self._trace("verify", mv["verdict"], f"model verifier {mv['reason'][:100]}")
                    if mv["verdict"] == "no":
                        model_failed = True
                        failures.append(f"model verifier: {mv['reason']}")
                except Exception as exc:
                    self._trace("verify", "failed", str(exc)[:100])
            elif (ok and self.model_verifier is not None
                    and not gate_skip_verifier):
                self._trace("verify", "skipped",
                            f"model verifier {verifier_mode}")
            # Passed means existence AND no failure mentions
            # it. A ledger file that exists but totals wrong
            # is not passed, so repair can rewrite it, while
            # a good file that passed every check is untouched.
            # A step that wrote its file is provisionally passed
            # above; a verification failure on that file must
            # take it back out, or the repair attempt would skip
            # the very step it has to fix. A mention is an exact
            # filename match, not a substring, so a data.txt
            # failure never takes a.txt out. Exception: a model
            # NO that names no deliverable at all. Crediting the
            # code-verified files to passed there would tell the
            # repair planner not to rewrite the very files the
            # model rejected, and the next attempt would skip
            # their steps entirely — a deadlock that re-verifies
            # identical files until attempts run out. A generic
            # rejection therefore keeps every deliverable
            # repairable; only a NO naming a file narrows repair
            # to that file.
            if model_failed and not any(
                    _mentions_file(msg, d) for msg in failures
                    for d in current_plan.deliverables):
                passed.difference_update(current_plan.deliverables)
            else:
                for name in list(passed):
                    if any(_mentions_file(msg, name) for msg in failures):
                        passed.discard(name)
                for name in verified:
                    if not any(_mentions_file(msg, name) for msg in failures):
                        passed.add(name)
            if (not ok and not model_failed
                    and not self._criterion_repaired and failures
                    and all(f.startswith("unverified criterion: ")
                            for f in failures)):
                # Unverifiable-only failure: no step failed, no file
                # is missing, the assertions themselves are the
                # problem. Rewrite them and re-verify the same
                # files; re-execution would only mint duplicates.
                self._criterion_repaired = True
                new_criteria = self._repair_criteria(
                    goal, criteria, failures)
                if new_criteria is not None:
                    self._trace("verify", "criterion-repair",
                                f"repaired {len(new_criteria)} criteria")
                    ok2, failures2, verified2 = verify_plan(
                        self.sandbox.workdir, current_plan.deliverables,
                        new_criteria)
                    if ok2:
                        current_plan.criteria = new_criteria
                        # The repaired set is the whole verification
                        # set now; stale step criteria must not be
                        # re-added on a later verification pass.
                        for step in current_plan.normalized_steps():
                            step.criteria = ""
                        ok, verify_failures, verified = True, [], verified2
                        failures = []
                        # The passed computation above already ran
                        # against the failed verification; credit
                        # the repaired verification here.
                        for name in verified2:
                            passed.add(name)
                    else:
                        self._trace("verify", "criterion-repair-failed",
                                    "; ".join(failures2)[:200])
            if ok and not model_failed:
                self._save_checkpoint(
                    "verified", goal, current_plan, passed,
                    verified=verified, attempt=attempt)
                break
            if self._criterion_repaired and failures and all(
                    f.startswith("unverified criterion: ") for f in failures):
                # Criterion repair is spent (one per run) and the
                # failure is still only uncheckable criteria: the
                # assertions themselves are the problem, and no
                # further repair exists for them. Re-executing
                # would fail identically, so fail fast with the
                # exact criteria named instead of burning the
                # remaining attempts. The user (or a new run with
                # checkable criteria) fixes the assertions; the
                # loop does not re-execute to prove it again.
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error=("criteria not checkable: "
                                        + "; ".join(failures))[:500],
                                 attempts=attempt)
            if attempt == max_attempts:
                return RunReport("failed", goal, current_plan, False, False,
                                 files=sorted(passed),
                                 error="; ".join(failures)[:500],
                                 attempts=attempt)
            # Clef recovery router: classify the failure before
            # replanning. retry_execution skips the repair plan
            # (the next attempt re-executes the same plan, passed
            # files untouched); acquire_capability adds a note
            # steering the repair toward the capability ladder;
            # escalate at high confidence stops the run instead
            # of burning attempts on an ambiguous failure.
            # Anything else, low confidence, no client, or a
            # router error, is the current repair path. Retry is
            # once per run: a misclassified persistent failure
            # must not burn every attempt on identical
            # re-execution.
            recovery_note = ""
            if getattr(self, "clef_client", None) is not None:
                try:
                    from .clef_recovery import route_failure
                    route = route_failure(
                        goal, attempt, failures,
                        getattr(self, "clef_client", None))
                    self._trace("recovery", route["route"],
                                f"clef recovery {route['route']} "
                                f"({route['confidence']:.2f})")
                    if (route["route"] == "escalate"
                            and route["confidence"] >= 0.8):
                        return RunReport(
                            "failed", goal, current_plan, False, False,
                            files=sorted(passed),
                            error=("recovery router escalated "
                                   f"({route['confidence']:.2f}); failure "
                                   "needs a user decision or cannot be "
                                   "safely routed")[:500],
                            attempts=attempt,
                            backend=self._backend_name())
                    if (route["route"] == "acquire_capability"
                            and route["confidence"] >= 0.6):
                        recovery_note = (
                            "\nRecovery router: a needed capability "
                            "(tool, package, skill, or MCP connection) "
                            "is suspected missing. Prefer finding, "
                            "building, or connecting it via the "
                            "capability ladder over rewriting the "
                            "artifact.")
                    elif (route["route"] == "retry_execution"
                            and route["confidence"] >= 0.6):
                        if not getattr(self, "_recovery_retried", False):
                            self._recovery_retried = True
                            self._trace("recovery", "retry",
                                        "re-executing plan unchanged")
                            continue
                        self._trace("recovery", "retry-spent",
                                    "retry already used; repairing instead")
                except Exception as exc:
                    self._trace("recovery", "skipped", str(exc)[:100])
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
            if recovery_note:
                context += recovery_note
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
        # Live approval is for live actions only. No caller populates
        # _live_actions today, and a plan of sandbox steps has none:
        # asking here would repeat the mode-stop approval for an
        # empty set, and force_ask would bypass even an Auto-Approve
        # grant. When live actions exist, the gate below still asks.
        if getattr(self, "_live_actions", None):
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
        else:
            self._trace("approval", "skipped", "no live actions")
            self._mission_approval(True)
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
        record = {"status": "done", "conversation_id": self.conversation_id,
                  "run_id": str(getattr(self, "run_id", "")),
                  "goal": goal, "deliverables": files,
                  "workflow": [{"id": step.id, "name": step.description,
                                "tool": step.tool,
                                "deliverable": step.deliverable,
                                "depends_on": list(step.depends_on or []),
                                "expected": step.criteria}
                               for step in current_plan.normalized_steps()],
                  "criteria": current_plan.criteria,
                  "backend": self._backend_name(),
                  "image": getattr(self.execution_backend, "image", "")
                  if self.execution_backend is not None else "",
                  "research": (self._research_report.to_dict()
                               if getattr(self, "_research_report", None) is not None else None),
                  "live": (live_record.to_dict() if live_record is not None else None)}
        (learn_dir / "last_run.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8")
        run_id = str(getattr(self, "run_id", ""))
        if run_id and run_id.replace("-", "").isalnum() and len(run_id) <= 128:
            run_dir = learn_dir / "runs"
            run_dir.mkdir(parents=True, exist_ok=True)
            temp = run_dir / f".{run_id}.tmp"
            target = run_dir / f"{run_id}.json"
            temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
            temp.replace(target)
        if self.memory is not None:
            episodic = self.memory.record_episodic(
                self.conversation_id, goal, {"deliverables": files})
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
                    _rolling_summary(self.home, self.conversation_id, goal, files, "done")
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
