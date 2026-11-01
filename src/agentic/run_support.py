"""Run tracing, approvals, missions, and checkpoint helpers."""

from __future__ import annotations

import json
import re
import time

from ..interface.approval import Decision
from ..system.model_client import Message
from .run_types import Plan, RunReport


class RunSupportMixin:
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

    def _trace(self, kind: str, status: str, detail: str = "", *,
               usage_phase: str | None = None,
               trace_key: str | None = None) -> None:
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
        key = trace_key or kind
        if status == "started":
            started[key] = time.monotonic()
        else:
            begin = started.pop(key, None)
            if begin is not None:
                seconds = round(time.monotonic() - begin, 1)
            for client in self._counting_clients():
                usage = client.take_phase_usage(usage_phase or kind)
                tokens_in += usage["tokens_in"]
                tokens_out += usage["tokens_out"]
                thinking_tokens += usage["thinking_tokens"]
        if self.trace is not None:
            self.trace.record(kind, status, detail, run_id=self.run_id)
            if callable(self.trace_callback):
                payload = {
                    "phase": kind, "status": status, "title": detail,
                    "detail": detail, "seconds": seconds,
                    "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "thinking_tokens": thinking_tokens,
                }
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
                f"{goal}: sandbox verification passed", self.conversation_id)
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
            decision = self.gate.ask(kind, title, body, **kwargs)
        else:
            with timer.approval_wait():
                decision = self.gate.ask(kind, title, body, **kwargs)
        if getattr(decision, "verdict", "") == "rejected":
            # A blocked action is evidence, not a silent failure:
            # the final report lists it for the morning. A timeout
            # is still awaiting, a human rejection is declined.
            note = f"{kind}: {title}"
            if getattr(decision, "reason", "") == "timeout":
                note += " (timed out, still awaiting)"
            else:
                note += " (declined)"
            blocked = getattr(self, "_blocked", None)
            if blocked is None:
                blocked = self._blocked = []
            blocked.append(note)
        return decision

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

