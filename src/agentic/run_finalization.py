"""Run usage aggregation and final report bookkeeping."""

from __future__ import annotations

from .run_types import RunReport


class RunFinalizationMixin:
    def _usage_totals(self) -> dict:
        """Finished-run usage across every counting tier.

        Sums the unwrapped counting clients, deduped, so fast-tier
        calls count even when the run's client is a plain client
        and only the router holds counting tiers. Basis is
        "actual" when every call reported provider usage — then
        in/out are the real counts — and "estimated" otherwise,
        when in/out are the chars//4 split. Think is always the real
        reasoning count: the estimate basis (prompt + reply
        text) never contains thinking text, so adding it cannot
        double-count it. Total is in + out + think on the
        chosen basis, so the footer breakdown always sums.
        """
        if self._counting_clients():
            clients = self._counting_clients()
            calls = sum(int(getattr(c, "calls", 0) or 0)
                        for c in clients)
            with_usage = sum(int(getattr(c, "calls_with_usage", 0) or 0)
                             for c in clients)
            basis = "actual" if calls and with_usage == calls else "estimated"
            if basis == "actual":
                in_tok = sum(int(getattr(c, "in_tokens_actual", 0) or 0)
                             for c in clients)
                out_tok = sum(int(getattr(c, "out_tokens_actual", 0) or 0)
                              for c in clients)
            else:
                in_tok = sum(int(getattr(c, "in_tokens_estimated", 0) or 0)
                             for c in clients)
                out_tok = sum(int(getattr(c, "out_tokens_estimated", 0) or 0)
                              for c in clients)
            think = sum(int(getattr(c, "thinking_tokens_total", 0) or 0)
                        for c in clients)
            in_actual = sum(int(getattr(c, "in_tokens_actual", 0) or 0)
                            for c in clients)
            out_actual = sum(int(getattr(c, "out_tokens_actual", 0) or 0)
                             for c in clients)
            return {"calls": calls, "in": in_tok, "out": out_tok,
                    "think": think, "total": in_tok + out_tok + think,
                    "basis": basis, "in_actual": in_actual,
                    "out_actual": out_actual}
        client = self.client
        if hasattr(client, "calls") and hasattr(client, "tokens_estimated"):
            return {"calls": int(client.calls), "in": 0, "out": 0,
                    "think": 0, "total": int(client.tokens_estimated),
                    "basis": "estimated", "in_actual": 0, "out_actual": 0}
        return {"calls": 0, "in": 0, "out": 0, "think": 0, "total": 0,
                "basis": "estimated", "in_actual": 0, "out_actual": 0}

    def _finalize(self, report: RunReport) -> RunReport:
        """Fill model-usage and adaptation fields, from the counting client if present."""
        timer = getattr(self, "_elapsed_timer", None)
        if timer is not None:
            timer.stop()
            report.elapsed_seconds = round(timer.elapsed_active(), 1)
            report.approval_wait_seconds = round(timer.approval_wait_seconds(), 1)
        totals = self._usage_totals()
        report.model_calls = totals["calls"]
        report.model_tokens_estimated = totals["total"]
        report.model_tokens_in_estimated = totals["in"]
        report.model_tokens_out_estimated = totals["out"]
        report.model_thinking_tokens = totals["think"]
        report.model_tokens_basis = totals.get("basis", "estimated")
        report.model_tokens_in_actual = totals.get("in_actual", 0)
        report.model_tokens_out_actual = totals.get("out_actual", 0)
        report.blocked = list(getattr(self, "_blocked", []))
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
                    self.conversation_id, report.goal,
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
                spawn_extraction(self.memory, self.conversation_id,
                                 f"User: {report.goal}\nAssistant: {report.chat_reply or ''}",
                                 self.client)
            except Exception:
                pass
        # Proven-run DAG learn, successful runs only, best
        # effort: a learn failure must never fail the run.
        if report.status == "done":
            try:
                from .dag_store import DagStore
                steps = report.plan.normalized_steps()
                nodes = []
                sequential = not bool(getattr(report.plan, "parallel_ok", False))
                for index, step in enumerate(steps):
                    node_id = step.id or f"step-{index + 1}"
                    dependencies = list(step.depends_on or [])
                    # A free-form plan is executed in listed order. Preserve
                    # that proven order when saving it as a reusable DAG;
                    # DAG-derived plans keep their explicit parallel edges.
                    if sequential and index:
                        previous_id = steps[index - 1].id or f"step-{index}"
                        if previous_id not in dependencies:
                            dependencies.append(previous_id)
                    nodes.append({"id": node_id, "tool": step.tool,
                                  "args": dict(step.args),
                                  "deliverable": step.deliverable,
                                  "depends_on": dependencies})
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
                spawn_extraction(self.memory, self.conversation_id,
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
        # Failure-lessons store: the same failure, with repair
        # linkage and recurrence, independent of memory. A
        # rejection or cancellation is not evidence the
        # approach was wrong, so those are not recorded.
        # Best effort, as above.
        if report.status == "failed":
            _err = (report.error or "").lower()
            if "reject" not in _err and "cancel" not in _err:
                try:
                    from ..memory.failure_lessons import FailureLessons
                    _tools = []
                    for _step in report.plan.normalized_steps():
                        if _step.tool not in _tools:
                            _tools.append(_step.tool)
                    FailureLessons(self.home).record_failure(
                        report.goal, report.error or "",
                        context={"tools": _tools,
                                 "deliverables": list(report.plan.deliverables or []),
                                 "files": list(report.files or [])})
                except Exception:
                    pass
        elif report.status == "done":
            try:
                from ..memory.failure_lessons import FailureLessons
                _store = FailureLessons(self.home)
                for _lesson in _store.lessons_for(report.goal, limit=3):
                    if not _lesson.get("repair"):
                        _store.record_repair(
                            _lesson["id"],
                            "A later run for a similar goal succeeded.")
                        break
            except Exception:
                pass
        # Diagnostics: a failed run records its error with
        # its run id, redacted, for `aidon diagnostics`.
        # Best effort, never fails the finished run.
        if report.status == "failed":
            try:
                from ..system.diagnostics import log_run_error
                log_run_error(self.home, str(getattr(self, "run_id", "") or ""),
                              "finalize", report.error or report.status)
            except Exception:
                pass
        # Mission skill harvest: ordinary runs test and
        # promote candidate skills, bounded by the run's
        # skill budget. Trust still needs owner approval,
        # and validated skills never enter plan prompts
        # until trusted. Best effort, never fails the run.
        if report.status == "done":
            try:
                from ..capabilities.skill_library import SkillLibrary
                _promoted = SkillLibrary(self.home).harvest_candidates(
                    getattr(self, "sandbox", None),
                    governor=getattr(self, "acquisition_governor", None))
                if _promoted:
                    report.promoted_skills = _promoted
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
        if report.status == "done" and getattr(self, "_capability_session_key", None):
            try:
                from ..interface.interview_store import InterviewStore
                InterviewStore(self.home).complete_session(self._capability_session_key)
            except Exception as exc:
                self._trace("capability", "failed", f"Could not close interview recovery state: {type(exc).__name__}")
        reg = getattr(self, "_checkpoint_registry", None)
        if reg is not None:
            try:
                reg.close()
            except Exception:
                pass
            self._checkpoint_registry = None
        return report
