"""Chat, goal lifecycle, and run snapshot handlers for WebUI."""

from __future__ import annotations

import threading

from .result_delivery import extract_result_delivery
from .triggers import TriggerEvent


class WebUIRunMixin:
    def _chat_guidance(self, conversation_id: str, run_guidance="") -> str:
        """Combine a conversation's saved brief with this run's guidance."""
        parts = []
        if conversation_id:
            try:
                saved = self.handle_brief_get(conversation_id) or {}
                brief = saved.get("brief", "") if isinstance(saved, dict) else ""
                if isinstance(brief, (dict, list)):
                    import json
                    brief = json.dumps(brief, ensure_ascii=False, indent=2)
                brief = str(brief or "").strip()
                if brief:
                    parts.append("Standing instructions for this chat:\n" + brief)
            except Exception:
                pass
        run_guidance = str(run_guidance or "").strip()
        if run_guidance:
            parts.append("Additional guidance for this run:\n" + run_guidance)
        return "\n\n".join(parts)

    def handle_chat(self, body: dict) -> dict:
        message = str(body.get("message", ""))
        control_reply = self._resolve_control(message)
        if control_reply is not None:
            return {"reply": control_reply}
        preapproval_reply = self._resolve_preapproval(message)
        if preapproval_reply is not None:
            return {"reply": preapproval_reply}
        memory_reply = self._resolve_memory_command(message)
        if memory_reply is not None:
            return {"reply": memory_reply}
        message, force_web = self._force_web_prefix(message)
        force_web = force_web or bool(body.get("force_web", False))
        if not message:
            return {"reply": "Usage: /web <question>"}
        message, delivery = extract_result_delivery(message)
        # A plain chat is global/unassigned unless a conversation id
        # is supplied with it.
        conversation_id = str(body.get("conversation_id", "") or "")
        guidance = self._chat_guidance(
            conversation_id, body.get("guidance", ""))
        replies: list[str] = []
        event = TriggerEvent(source="webui", kind="chat", goal=message,
                             conversation_id=conversation_id,
                             payload={**({"result_delivery": delivery.__dict__}
                                         if delivery else {}),
                                      "guidance": guidance,
                                      "force_web": force_web},
                             reply=replies.append)
        try:
            self.dispatcher.dispatch(event)
        except Exception as exc:
            return {"reply": f"Error: {exc}"}
        return {"reply": replies[0] if replies else "Received."}

    def ask_question(self, run_id: str, conversation_id: str, question: str,
                     also_keys: tuple[str, ...] = ()) -> str:
        """Block the run thread until the owner answers in chat.

        The question is posted as a chat event so the owner sees it,
        and run_snapshot reports it as waiting_question so the
        composer stays usable. Questions carry no 900s clock (chat
        is for conversation, cards are for decisions); the wait ends
        on an answer or on run cancellation. also_keys registers
        the same waiter under linked surfaces, so an answer from
        Slack or WhatsApp resolves a WebUI run's question and
        vice versa (see surfaces.waiter_keys_*).
        """
        lab_question = str(question).startswith("[AIDON_LAB] ")
        if lab_question:
            question = str(question)[len("[AIDON_LAB] "):]
        from .interview_store import InterviewStore
        journal = InterviewStore(self.home)
        turn_id = journal.question(run_id, conversation_id, str(question), lab_question)
        event = threading.Event()
        keys = [conversation_id] + [key for key in also_keys if key and key != conversation_id]
        waiter = {"run_id": run_id, "conversation_id": conversation_id,
                  "question": str(question), "event": event, "answer": None,
                  "keys": keys, "lab": lab_question, "turn_id": turn_id}
        with self._question_lock:
            self._question_waiters[run_id] = waiter
            for key in keys:
                self._question_by_conversation[key] = waiter
        record = self._web_runs.get(run_id)
        if record is not None:
            record["events"].append({"kind": "lab-question" if lab_question else "aidon", "text": str(question), "turn_id": turn_id})
        try:
            while not event.wait(1.0):
                if (self.run_control is not None
                        and self.run_control.is_cancelled(run_id)):
                    return ("QUESTION_CANCELLED: the run was cancelled "
                            "while waiting for an answer.")
            return waiter["answer"] if waiter["answer"] is not None else ""
        finally:
            with self._question_lock:
                if self._question_waiters.get(run_id) is waiter:
                    del self._question_waiters[run_id]
                for key in waiter.get("keys", [conversation_id]):
                    if self._question_by_conversation.get(key) is waiter:
                        del self._question_by_conversation[key]

    def answer_question(self, conversation_id: str, answer: str) -> str | None:
        """Deliver a chat answer to the waiting run in this conversation."""
        with self._question_lock:
            waiter = self._question_by_conversation.get(conversation_id)
            if waiter is None:
                return None
            from .interview_store import InterviewStore
            InterviewStore(self.home).answer(waiter["turn_id"], str(answer))
            if waiter.get("lab"):
                record = self._web_runs.get(waiter["run_id"])
                if record is not None:
                    record["events"].append({"kind": "lab-answer", "text": str(answer), "turn_id": waiter["turn_id"]})
            waiter["answer"] = str(answer)
            waiter["event"].set()
            return waiter["run_id"]

    def waiting_question(self, run_id: str) -> str | None:
        with self._question_lock:
            waiter = self._question_waiters.get(run_id)
            return waiter["question"] if waiter else None

    def _mirror_command(self, conversation_id: str, goal: str) -> dict | None:
        """Handle a /mirror command in WebUI chat; None when not one."""
        from .surfaces import (link_slack, unlink_slack, set_whatsapp_mirror,
                               mirror_status_text, parse_mirror_command)
        command = parse_mirror_command(goal)
        if command is None:
            return None
        action = command["action"]
        if action == "link_slack":
            link_slack(self.home, conversation_id, command["channel"])
            text = f"Linked this conversation to Slack #{command['channel']}."
        elif action == "unlink_slack":
            removed = unlink_slack(self.home, conversation_id)
            text = ("Slack link removed." if removed
                    else "No Slack link to remove.")
        elif action == "whatsapp":
            set_whatsapp_mirror(self.home, conversation_id, command["enabled"])
            text = ("WhatsApp mirror on for this conversation."
                    if command["enabled"]
                    else "WhatsApp mirror off for this conversation.")
        elif action == "link_webui":
            text = ("You are already in this conversation; from Slack, "
                    "use /mirror webui <id> to link a channel here.")
        else:
            text = mirror_status_text(self.home, conversation_id=conversation_id)
        try:
            self._conversations_db().record_exchange(conversation_id, "assistant", text)
        except Exception:
            pass
        if self.memory is not None:
            try:
                self.memory.record_stm("assistant", text,
                                       conversation_id=conversation_id)
            except Exception:
                pass
        from .run_control import new_run_id
        run_id = new_run_id()
        self._web_runs[run_id] = {"run_id": run_id, "goal": goal,
                                  "conversation_id": conversation_id, "status": "done",
                                  "events": [{"kind": "aidon-final", "text": text,
                                              "stream_id": ""}], "trace": []}
        return {"run_id": run_id, "conversation_id": conversation_id, "goal": goal}

    def start_goal(self, body: dict) -> dict:
        """Start the same dispatcher path used by messaging triggers."""
        from .run_control import new_run_id
        goal = str(body.get("goal", "")).strip()
        if not goal:
            return {"error": "goal is required"}
        goal, force_web = self._force_web_prefix(goal)
        if not goal:
            return {"error": "a web question is required"}
        force_web = force_web or bool(body.get("force_web", False))
        goal, delivery = extract_result_delivery(goal)
        raw_goal = goal
        conversation_id = str(body.get("conversation_id", "") or "")
        skill_source_run_id = str(body.get("skill_source_run_id", "") or "")
        if skill_source_run_id and not conversation_id:
            return {"error": "a verified skill source requires its original conversation"}
        if skill_source_run_id and not self.handle_skill_source(
                skill_source_run_id, conversation_id):
            return {"error": "skills can only be drafted from a verified successful run in this conversation"}
        conversation_db = None
        prior_history = []
        with self._question_lock:
            pending_waiter = self._question_by_conversation.get(conversation_id)
            answering_lab = bool(pending_waiter and pending_waiter.get("lab"))
        try:
            conversation_db = self._conversations_db()
            # The browser can retain an ID that has no server-side row
            # (for example after switching homes or restoring old local
            # storage). Ensure supplied IDs are registered too, so history
            # listing and refresh restore the turns written below.
            conversation_id = conversation_db.create_conversation(
                conversation_id or None)["id"]
            prior_history = conversation_db.history(conversation_id, limit=8)
            if not answering_lab:
                conversation_db.record_exchange(conversation_id, "user", raw_goal)
        except Exception:
            # A run must still work if the optional conversation store is
            # unavailable; the durable run record remains authoritative.
            conversation_db = None
        if self.memory is not None:
            try:
                self.memory.record_stm(
                    "user", goal,
                    attachments=body.get("image_paths") or [],
                    conversation_id=conversation_id,
                )
            except Exception:
                pass
        # A message sent while this conversation's run waits for a
        # question IS the answer, not a new goal: the composer is
        # disabled while a run works and enabled only while it waits,
        # so a goal arriving here can only be an answer. Return the
        # same run id, as the frontend's answer path expects.
        answered_run = self.answer_question(conversation_id, goal)
        if answered_run is not None:
            return {"run_id": answered_run, "conversation_id": conversation_id,
                    "goal": goal, "lab_answer": answering_lab}
        resume_interview = goal.strip().lower() == "/resume-interview"
        if resume_interview:
            from .interview_store import InterviewStore
            try:
                saved = InterviewStore(self.home).unfinished_for_conversation(conversation_id)
            except ValueError as exc:
                return self._simple_command_response(conversation_id, raw_goal, str(exc))
            if not saved:
                return self._simple_command_response(conversation_id, raw_goal, "There is no unfinished interview in this conversation.")
            goal = saved["goal"]
            raw_goal = goal
        if not resume_interview:
            goal = self._merge_correction_followup(raw_goal, prior_history)
        mirror_response = self._mirror_command(conversation_id, goal)
        if mirror_response is not None:
            return mirror_response
        from .mode_stops import latest_mode_stop, load_mode_stop
        from .surfaces import (HELP_TEXT, mode_enabled, parse_mode_command,
                               parse_proceed_command, parse_research_command,
                               parse_think_command, research_enabled,
                               set_mode, think_enabled, toggle_research,
                               toggle_think)
        if goal.strip() == "/help":
            return self._simple_command_response(conversation_id, goal,
                                                 HELP_TEXT)
        think_max = False
        think_cmd = parse_think_command(goal)
        if think_cmd is not None:
            if think_cmd["action"] == "toggle":
                state = toggle_think(self.home, conversation_id)
                return self._simple_command_response(
                    conversation_id, goal,
                    f"Maximum thinking {'on' if state else 'off'} for this conversation.")
            goal = think_cmd["goal"]
            think_max = True
        think_max = think_max or think_enabled(self.home, conversation_id)
        research_mode = False
        research_cmd = parse_research_command(goal)
        if research_cmd is not None:
            if research_cmd["action"] == "toggle":
                state = toggle_research(self.home, conversation_id)
                return self._simple_command_response(
                    conversation_id, goal,
                    f"Deep-research mode {'on' if state else 'off'} for this conversation.")
            goal = research_cmd["goal"]
            research_mode = True
        research_mode = research_mode or research_enabled(self.home, conversation_id)
        # Modes: a typed one-shot wins, then the composer's chooser
        # (payload "mode"), then the sticky flag for the conversation.
        # /proceed resumes a stopped run instead of starting a goal.
        mode = "plan" if resume_interview else None
        resume_stop_id = ""
        resume_spec = None
        resume_plan = None
        mode_cmd = parse_mode_command(goal)
        if mode_cmd is not None:
            if mode_cmd["action"] == "set":
                set_mode(self.home, conversation_id, mode_cmd["mode"])
                if mode_cmd["mode"] == "execute":
                    _mtext = "Execute mode on for this conversation — goals run directly."
                else:
                    _mtext = f"{mode_cmd['mode'].capitalize()} mode on for this conversation."
                return self._simple_command_response(conversation_id, goal, _mtext)
            goal = mode_cmd["goal"]
            mode = mode_cmd["mode"]
        proceed_cmd = parse_proceed_command(goal)
        if proceed_cmd is not None:
            stop = (load_mode_stop(self.home, proceed_cmd["stop_id"])
                    if proceed_cmd["stop_id"]
                    else latest_mode_stop(self.home, conversation_id))
            if stop is None:
                if proceed_cmd["stop_id"]:
                    _ptext = f"No stopped run with id {proceed_cmd['stop_id']}."
                else:
                    _ptext = "No stopped run to proceed with."
                return self._simple_command_response(conversation_id, goal, _ptext)
            goal = stop["goal"]
            # Mode chain: Ask -> Plan -> Execute. Ask is for
            # beginners and detail, so its Proceed builds a plan
            # for approval; only the plan's Proceed executes. A
            # Plan stop proceeds straight to execution, as before.
            mode = "plan" if stop.get("mode") == "ask" else "execute"
            resume_stop_id = stop["stop_id"]
            resume_spec = stop.get("spec")
            resume_plan = stop.get("plan")
        if mode is None and str(body.get("mode", "") or "") in ("ask", "plan", "execute"):
            mode = str(body.get("mode"))
        if mode is None:
            mode = mode_enabled(self.home, conversation_id) or "plan"
        run_id = new_run_id()
        if self.run_control is not None:
            self.run_control.register(run_id, goal, conversation_id)
        record = {"run_id": run_id, "goal": goal, "conversation_id": conversation_id,
                  "status": "running", "events": [], "trace": []}
        self._web_runs[run_id] = record
        stream_state = {"text": "", "active": False}

        def stream_delta(delta):
            text = str(delta or "")
            if not text:
                return
            stream_state["active"] = True
            stream_state["text"] += text
            record["events"].append({"kind": "aidon-stream",
                                     "stream_id": run_id, "delta": text})

        def route_update(route):
            if not route or len(route) < 2:
                return
            record["route"] = {"group": str(route[0]),
                               "confidence": round(float(route[1]), 3)}

        def trace_update(item):
            if isinstance(item, dict):
                record["trace"].append(dict(item))

        def question_callback(question):
            # Fan out to linked surfaces now, and register the
            # waiter under their keys, so an answer from Slack or
            # WhatsApp resolves this WebUI run's question.
            from .surfaces import (slack_for_conversation,
                                   waiter_keys_for_conversation,
                                   whatsapp_mirrored_for_conversation)
            owner = ""
            try:
                owner = str(self.messaging_config.whatsapp_owner or "")
            except Exception:
                pass
            channel = slack_for_conversation(self.home, conversation_id)
            if channel and self.slack_sender is not None:
                try:
                    self.slack_sender(channel, str(question))
                except Exception:
                    pass
            if (owner and whatsapp_mirrored_for_conversation(self.home, conversation_id)
                    and self.whatsapp_sender is not None):
                try:
                    self.whatsapp_sender(owner, str(question))
                except Exception:
                    pass
            return self.ask_question(run_id, conversation_id, question,
                                     also_keys=tuple(waiter_keys_for_conversation(
                                         self.home, conversation_id, owner)))

        def reply(text):
            record["events"].append({"kind": "aidon-final", "text": str(text),
                                     "stream_id": run_id if stream_state["active"] else ""})
            if conversation_db is not None:
                try:
                    conversation_db.record_exchange(
                        conversation_id, "assistant", str(text),
                        route=record.get("route"))
                except Exception:
                    pass
            if self.memory is not None:
                try:
                    self.memory.record_stm(
                        "assistant", str(text),
                        conversation_id=conversation_id)
                except Exception:
                    pass

        payload = {"run_id": run_id,
                   "guidance": self._chat_guidance(
                       conversation_id, body.get("guidance", "")),
                   "force_web": force_web,
                   "think_max": think_max,
                   "research_mode": research_mode,
                   "_stream_callback": stream_delta,
                   "_route_callback": route_update,
                   "_trace_callback": trace_update,
                   "_question_callback": question_callback,
                   "resume_interview": resume_interview,
                   "skill_source_run_id": skill_source_run_id,
                   "result_delivery": delivery.__dict__ if delivery else {}}
        image_paths = [str(path) for path in (body.get("image_paths") or [])
                       if str(path).strip()]
        if image_paths:
            payload["image_paths"] = image_paths
        payload["mode"] = mode
        if resume_stop_id:
            payload["resume_stop_id"] = resume_stop_id
        if isinstance(resume_spec, dict):
            payload["resume_spec"] = resume_spec
        if isinstance(resume_plan, dict):
            payload["resume_plan"] = resume_plan
        event = TriggerEvent(source="webui", kind="goal", goal=goal,
                             conversation_id=conversation_id,
                             payload={**payload, "conversation_id": conversation_id},
                             reply=reply)

        def dispatch():
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                record["events"].append({"kind": "aidon-final",
                                         "text": f"Error: {exc}"})
                if self.run_control is not None:
                    self.run_control.set_status(run_id, "failed")

        threading.Thread(target=dispatch, daemon=True,
                         name=f"aidon-run-{run_id}").start()
        return {"run_id": run_id, "conversation_id": conversation_id,
                "goal": goal}

    def run_snapshot(self, run_id: str, since: int = 0, trace_since: int = 0) -> dict | None:
        record = self._web_runs.get(run_id)
        control = self.run_control.get(run_id) if self.run_control is not None else None
        if record is None and control is None:
            return None
        out = dict(record or {})
        if control:
            terminal = control.get("status") in {
                "done", "failed", "rejected", "cancelled", "stopped"}
            out.update({"status": control.get("status", out.get("status", "running")),
                        "goal": control.get("goal", out.get("goal", "")),
                        "stage": ("done" if terminal else
                                   control.get("stage", out.get("stage", "interpret")))})
        out.setdefault("events", [])
        out.setdefault("trace", [])
        if record is None and control is not None:
            from .interview_store import InterviewStore
            saved_turns = InterviewStore(self.home).transcript(run_id)
            if saved_turns:
                out["status"] = "interrupted"
                out["events"] = []
                for turn in saved_turns:
                    out["events"].append({"kind": "lab-question" if turn["lab"] else "aidon",
                                          "text": turn["question"]})
                    if turn["answer"] is not None:
                        out["events"].append({"kind": "lab-answer" if turn["lab"] else "user",
                                              "text": turn["answer"]})
                out["events"].append({"kind": "aidon-final", "text":
                    "This interview was interrupted. Your saved answers are preserved. Send /resume-interview to resume it."})
        # The browser polls incrementally. Returning the complete history on
        # every poll caused each old assistant event to be rendered again,
        # which looked like an infinite model loop.
        all_events = out["events"]
        all_trace = out["trace"]
        out["event_total"] = len(all_events)
        out["trace_total"] = len(all_trace)
        out["events"] = all_events[max(0, int(since)):]
        out["trace"] = all_trace[max(0, int(trace_since)):]
        out["waiting_question"] = self.waiting_question(run_id)
        with self._question_lock:
            waiter = self._question_waiters.get(run_id)
            out["waiting_lab"] = bool(waiter and waiter.get("lab"))
        pending = self.webui_gate.pending_cards()
        out["pending"] = pending[0] if pending else None
        return out
