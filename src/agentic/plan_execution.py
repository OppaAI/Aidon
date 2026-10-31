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
