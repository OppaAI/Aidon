"""CLI entry point for configuring and serving the WebUI."""

from __future__ import annotations

import os
import threading

from .runtime import (
    _make_governor,
    _make_web_researcher,
    _network_available,
    _run_sandbox,
    _startup_style,
    make_deep_researcher,
)


def run_webui(args, config, memory, client, model_router=None) -> int:
    from .goal_tools import _goal_extra_tools
    from ..interface.messaging import MessagingConfig, send_slack, send_whatsapp
    from ..interface.triggers import TriggerDispatcher, TriggerEvent
    from ..interface.v1 import AuthStore
    from ..interface.webui import WebUIServer
    msg = MessagingConfig.from_env()
    # Override from Config, which already read the old names.
    msg.whatsapp_token = config.whatsapp_token or msg.whatsapp_token
    msg.whatsapp_phone_id = config.whatsapp_phone_id or msg.whatsapp_phone_id
    msg.whatsapp_verify_token = config.whatsapp_verify_token or msg.whatsapp_verify_token
    msg.whatsapp_app_secret = config.whatsapp_app_secret or msg.whatsapp_app_secret
    msg.whatsapp_owner = config.whatsapp_owner or msg.whatsapp_owner
    msg.slack_bot_token = config.slack_bot_token or msg.slack_bot_token
    msg.slack_signing_secret = config.slack_signing_secret or msg.slack_signing_secret
    msg.slack_outbound_channel = (config.slack_outbound_channel
                                  or msg.slack_outbound_channel)
    dispatcher = TriggerDispatcher()
    # Goal handler, wired here, not echo: WebUI chat,
    # WhatsApp, and Slack all run the same GoalRunner,
    # with the WebUI approval gate, so a card in the
    # browser, or approve <id> on a message, resolves
    # a run that started on any surface.

    def whatsapp_sender(to: str, text: str):
        result = send_whatsapp(msg, to, text)
        if not result["ok"]:
            print(f"WhatsApp send failed: {result['error']}")
        return result

    def slack_sender(channel: str, text: str, thread_ts=None):
        result = send_slack(msg, channel, text, thread_ts=thread_ts)
        if not result["ok"]:
            print(f"Slack send failed for {channel}: {result['error']}")
        return result

    from ..interface.reply_delivery import ReplySender
    _whatsapp_transport = whatsapp_sender
    whatsapp_sender = ReplySender(
        config.home, "whatsapp",
        lambda to, text, thread_ts: _whatsapp_transport(to, text))
    slack_sender = ReplySender(config.home, "slack", slack_sender)

    from ..interface.messaging import download_whatsapp_media
    from ..interface.run_control import RunControl
    run_control = RunControl(config.home)
    server = WebUIServer(config.home, dispatcher=dispatcher,
                         auth_store=AuthStore(config.home), memory=memory,
                         messaging_config=msg,
                         whatsapp_sender=whatsapp_sender,
                         slack_sender=slack_sender,
                         webhook_only=args.webhook_only,
                         media_downloader=lambda media_id: download_whatsapp_media(msg, media_id),
                         run_control=run_control,
                         webui_token=config.webui_token or None,
                         transcribe_key=config.transcribe_api_key or config.api_key or None,
                         tts_key=config.fish_api_key or config.api_key or None,
                         config=config)
    server.voice_announce = config.voice_announce

    def _goal_handler(event):
        from ..agentic.loop import GoalRunner, format_run_stats
        from ..interface.run_control import new_run_id
        from ..agentic.budgets import Budget
        from ..agentic.trace import TraceRecorder
        from ..agentic.verifier_model import ModelVerifier
        from ..capabilities.plugins import PluginRegistry
        from ..interface.scheduler_loop import NudgeTracker
        run_id = str(event.payload.get("run_id", "")) or new_run_id()
        run_control.register(run_id, event.goal, event.conversation_id)
        # Each goal gets its own timestamped workspace directory. This
        # prevents unrelated runs from colliding on filenames and keeps
        # the user-visible artifact path contextual instead of exposing
        # a generic ``workdir`` directory.
        run_sandbox = _run_sandbox(config, run_id)
        from ..agentic.conversation_workspace import ConversationWorkspace
        workspace_store = (ConversationWorkspace(config.home, event.conversation_id)
                           if event.conversation_id else None)
        prior_workspace = (workspace_store.stage(run_sandbox.workdir)
                           if workspace_store is not None else {})
        # Full runner, same as the CLI goal run, not a thin runner:
        # budget, trace, model verifier, plugins, execution backend,
        # semantic router skip, and autonomy plus nudge trackers
        # on the WebUI gate, so a WebUI, WhatsApp, or Slack run
        # is the same run as a CLI run, with the same checks.
        server.webui_gate.nudge_tracker = NudgeTracker(
            nudge_minutes=config.approval_nudge_minutes,
            channel=config.approval_nudge_channel)
        from ..interface.autonomy import policy_for
        server.webui_gate.gate.policy = policy_for(
            config.home,
            threshold=config.autonomy_threshold,
            decay_days=config.autonomy_decay_days,
        )
        server.webui_gate.autonomy_tracker = None
        server.webui_gate.slack_sender = slack_sender
        server.webui_gate.whatsapp_sender = whatsapp_sender
        plugins = PluginRegistry(config.home)
        plugins.load_all()
        budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                        max_seconds=config.budget_seconds)
        trace = TraceRecorder(config.home, conversation_id=event.conversation_id)
        try:
            from ..capabilities.execution import make_backend
            execution_backend = make_backend(
                config.execution_backend, run_sandbox,
                image=config.docker_image, allow_network=config.docker_network,
                vm_host=config.vm_host, vm_user=config.vm_user or None,
                vm_sudo_password=config.vm_sudo_password or None,
                vm_use_sudo=config.vm_use_sudo,
                modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                modal_memory_mb=config.modal_memory_mb)
        except Exception:
            execution_backend = None
        try:
            from ..agentic.clef_router import build_router as _build_router
            semantic_router = _build_router(
                config, memory.embedder,
                config.home / 'semantic_router_vectors.json')
        except Exception:
            semantic_router = None
        # Clef decision client for capability triage in run().
        # None when no URL is configured: triage is skipped and
        # the run behaves exactly as before.
        _clef_client = None
        try:
            if getattr(config, "clef_url", ""):
                from ..agentic.clef_client import ClefClient as _ClefClient
                _clef_client = _ClefClient(
                    config.clef_url,
                    model=getattr(config, "clef_model", "") or "Cloudflare/clef-flash",
                    key=getattr(config, "clef_key", "") or "",
                    secret=getattr(config, "clef_secret", "") or "")
        except Exception:
            _clef_client = None
        conversation_context = ""
        conversation_id = str(event.payload.get("conversation_id", "") or event.conversation_id or "")
        if conversation_id:
            try:
                history = server._conversations_db().history(conversation_id, limit=8)
                if (history and history[-1].get("role") == "user"
                        and history[-1].get("text") == event.goal):
                    history = history[:-1]
                conversation_context = "\n".join(
                    f"{item.get('role', 'user')}: {str(item.get('text', ''))[:1200]}"
                    for item in history
                )
            except Exception:
                conversation_context = ""
        if prior_workspace:
            prior_artifacts = prior_workspace.get("artifacts", [])
            prior_flow = prior_workspace.get("workflow", {})
        else:
            prior_artifacts, prior_flow = [], {}
        if prior_artifacts or prior_flow.get("goal"):
            lines = ["Prior completed work for this conversation is reference context only."]
            if prior_flow.get("goal"):
                lines.append(f"Previous goal: {prior_flow['goal']}")
            if prior_flow.get("summary"):
                lines.append(f"Previous workflow summary: {prior_flow['summary']}")
            if prior_flow.get("steps"):
                lines.append("Previous workflow steps: " + "; ".join(
                    str(step.get("description") or step.get("tool") or "step")
                    for step in prior_flow["steps"][:12]))
            if prior_artifacts:
                lines.append("Previously verified artifacts available in this run's workspace: "
                             + ", ".join(prior_artifacts))
            lines.append("Follow the current request; use or change prior artifacts only as requested.")
            conversation_context = (conversation_context + "\n\n"
                                    + "\n".join(lines)).strip()
        if event.payload.get("image_paths"):
            from ..agentic.context_policy import explicitly_refers_to_prior_context
            if not explicitly_refers_to_prior_context(event.goal):
                # Keep old turns out of both the goal runner and the
                # researcher closure for a standalone image question.
                conversation_context = ""
        try:
            from ..interface.preapproval import PreapprovalStore
            from ..interface.preapproval_gate import PreapprovalGate
            _gate = PreapprovalGate(server.webui_gate.gate, PreapprovalStore(config.home), event.conversation_id)
        except Exception:
            _gate = server.webui_gate.gate
        _web_researcher = _make_web_researcher(config, conversation_context)
        # Slack/WhatsApp runs have no WebUI waiter callback in the
        # payload, so build one: send the question straight to the
        # surface (not via the reply list, which is only flushed
        # after the run ends), then block in the shared waiter
        # until the owner's next message in that channel arrives
        # as the answer. Without a channel/sender key there is no
        # waiter, and ask_user says so honestly.
        _question_callback = None
        _qkey = ""
        if event.source == "slack":
            _qkey = "slack:" + str(event.payload.get("channel", ""))
        elif event.source == "whatsapp":
            _qkey = "whatsapp:" + str(event.payload.get("from", ""))
        if _qkey and not _qkey.endswith(":"):
            def _question_callback(question, _key=_qkey):
                from ..interface.surfaces import waiter_keys_for_slack
                also: list[str] = []
                if event.source == "slack":
                    if slack_sender is not None:
                        try:
                            slack_sender(
                                str(event.payload.get("channel", "")),
                                str(question),
                                event.payload.get("thread_ts"))
                        except Exception:
                            pass
                    # The thread is visible in WebUI under
                    # slack:<channel>, question included.
                    try:
                        server._conversations_db().record_exchange(
                            "slack:" + str(event.payload.get("channel", "")),
                            "assistant", str(question))
                    except Exception:
                        pass
                    also = waiter_keys_for_slack(
                        config.home, str(event.payload.get("channel", "")),
                        str(msg.whatsapp_owner or ""))
                    # A linked WebUI conversation shows the
                    # question, so the owner can answer there.
                    for key in also:
                        if key.startswith("whatsapp:"):
                            continue
                        try:
                            server._conversations_db().record_exchange(
                                key, "assistant", str(question))
                        except Exception:
                            pass
                    for key in also:
                        if key.startswith("whatsapp:") and whatsapp_sender is not None:
                            try:
                                whatsapp_sender(key[len("whatsapp:"):], str(question))
                            except Exception:
                                pass
                elif event.source == "whatsapp" and whatsapp_sender is not None:
                    try:
                        whatsapp_sender(
                            str(event.payload.get("from", "")),
                            str(question))
                    except Exception:
                        pass
                return server.ask_question(run_id, _key, question, also_keys=tuple(also))
        def _say(prompt):
            from ..system.model_client import Message as _Message
            messages = [_Message("user", prompt)]
            if model_router is not None:
                try:
                    return model_router.chat("respond", messages)
                except TypeError:
                    return model_router.chat(messages)
            return client.chat(messages)

        def _phrase(kind):
            from ..interface.phrasing import phrase_with_model as _pwm
            return _pwm(kind, _say)

        # Approval cards carry this run's context so the
        # WebUI gate can deliver a copy to WhatsApp when
        # the run warrants one: a run that started on
        # WhatsApp, or a WebUI/Slack conversation the
        # owner mirrored to WhatsApp. Mirroring is the
        # opt-in; an unmirrored run gets no WhatsApp card.
        _whatsapp_to = None
        if event.source == "whatsapp":
            _whatsapp_to = str(event.payload.get("from", "") or "")
        elif msg.whatsapp_owner:
            from ..interface.surfaces import (
                whatsapp_mirrored_for_conversation,
                whatsapp_mirrored_for_slack)
            if event.source == "webui" and conversation_id and \
                    whatsapp_mirrored_for_conversation(
                        config.home, conversation_id):
                _whatsapp_to = msg.whatsapp_owner
            elif event.source == "slack" and \
                    whatsapp_mirrored_for_slack(
                        config.home,
                        str(event.payload.get("channel", ""))):
                _whatsapp_to = msg.whatsapp_owner
        _gate_context = {"run_id": run_id,
                         "conversation_id": conversation_id,
                         "source": event.source}
        if event.source == "slack":
            _gate_context["slack_channel"] = str(event.payload.get("channel", ""))
            _gate_context["slack_thread_ts"] = event.payload.get("thread_ts")
        if _whatsapp_to:
            _gate_context["whatsapp_to"] = _whatsapp_to

        # Mode resume + conversation key for the stop record.
        _conversation_key = ""
        if event.source == "webui":
            _conversation_key = str(event.payload.get("conversation_id", "") or "")
        elif event.source == "slack":
            _conversation_key = "slack:" + str(event.payload.get("channel", "") or "")
        elif event.source == "whatsapp":
            _conversation_key = "whatsapp:" + str(event.payload.get("from", "") or "")
        _resume_stop_id = str(event.payload.get("resume_stop_id", "") or "")
        _resume_plan = None
        _resume_spec = None
        if isinstance(event.payload.get("resume_plan"), dict):
            _rp = event.payload["resume_plan"]
            if _rp.get("steps"):
                from ..agentic.loop import Plan as _ResumePlan
                _resume_plan = _ResumePlan(
                    steps=_rp.get("steps", []),
                    deliverables=[str(d) for d in _rp.get("deliverables", [])],
                    criteria=[str(c) for c in _rp.get("criteria", [])])
        if isinstance(event.payload.get("resume_spec"), dict):
            from ..agentic.interview import Spec as _ResumeSpec
            _resume_spec = _ResumeSpec.from_dict(event.payload["resume_spec"])
        # Mission budget key: a proceed rejoins the stopped
        # run's record via the stop; a fresh run gets a fresh
        # key, so a later mission never inherits an earlier
        # mission's spent budget. Stops saved before keys
        # existed fall back to the (chat, goal) hash.
        _budget_key = None
        try:
            if _resume_stop_id:
                from ..interface.mode_stops import load_mode_stop as _site_load_stop
                _stop = _site_load_stop(config.home, _resume_stop_id)
                _budget_key = (_stop or {}).get("budget_key") or None
                if not _budget_key:
                    from ..capabilities.acquisition_governor import budget_key_for
                    _budget_key = budget_key_for(_conversation_key, event.goal)
            else:
                import uuid as _budget_uuid
                _budget_key = f"mission:{_budget_uuid.uuid4().hex[:16]}"
        except Exception:
            _budget_key = None
        _governor = _make_governor(config, _budget_key)
        _skill_source_run_id = str(event.payload.get("skill_source_run_id", "") or "")
        _allow_skill_adoption = False
        if event.source == "webui" and _skill_source_run_id:
            try:
                _skill_source = server.handle_skill_source(
                    _skill_source_run_id, event.conversation_id)
                _allow_skill_adoption = bool(_skill_source)
            except Exception:
                _allow_skill_adoption = False
        runner = GoalRunner(client, run_sandbox, _gate,
                            config.home, memory=memory,
                            conversation_id=event.conversation_id, plugins=plugins,
                            execution_backend=execution_backend, budget=budget,
                            trace=trace, model_verifier=ModelVerifier(client),
                            kernel_mode=config.kernel_mode,
                            semantic_router=semantic_router,
                            clef_client=_clef_client,
                            use_semantic_router_skip=True,
                            run_id=run_id, run_control=run_control,
                            researcher=_web_researcher, extra_tools=_goal_extra_tools(config, run_sandbox.workdir, gate=_gate, researcher=_web_researcher, question_answer=event.payload.get("_question_callback") or _question_callback, phrase=_phrase, model_chat=lambda messages: client.chat(messages), governor=_governor, plugin_registry=plugins, gate_context=_gate_context, test_backend_factory=lambda: make_backend(config.execution_backend, run_sandbox, image=config.docker_image, allow_network=False, modal_app=config.modal_app, modal_cpu=config.modal_cpu, modal_memory_mb=config.modal_memory_mb), allow_skill_adoption=_allow_skill_adoption), router=model_router, versioned_writes=True, network_available=_network_available(execution_backend, run_sandbox), image_paths=event.payload.get("image_paths") or [], force_web=bool(event.payload.get("force_web")), think_max=bool(event.payload.get("think_max")), research_mode=bool(event.payload.get("research_mode")), deep_researcher=make_deep_researcher(config), stream_callback=event.payload.get("_stream_callback"), conversation_context=conversation_context, guidance=str(event.payload.get("guidance", "") or ""), route_callback=event.payload.get("_route_callback"), trace_callback=event.payload.get("_trace_callback"), gate_context=_gate_context, mode=event.payload.get("mode"), question_ask=event.payload.get("_question_callback") or _question_callback, conversation_key=_conversation_key, resume_plan=_resume_plan, acquisition_governor=_governor, resume_interview=bool(event.payload.get("resume_interview")), conversation_thread_id=str(event.payload.get("thread_ts", "") or ""))
        def _deliver_files(files, target, *, thread_ts=""):
            """Upload run deliverables as bytes to a Slack/WhatsApp target.

            target is ("slack", channel) or ("whatsapp", number).
            Slack and WhatsApp cannot open localhost file links,
            so a run's files reach those surfaces as uploads,
            not links. Paths are confined to the run workdir;
            anything outside it, or not a file, is skipped.
            Failures are reported as a reply, never silent.
            """
            if not files or not target:
                return
            from pathlib import Path as _P
            root = _P(run_sandbox.workdir).resolve()
            kind, recipient = target
            if not recipient:
                return
            for name in files:
                candidate = (_P(name) if _P(name).is_absolute()
                             else root / str(name))
                try:
                    resolved = candidate.resolve()
                except Exception:
                    continue
                if resolved != root and root not in resolved.parents:
                    continue
                if not resolved.is_file():
                    continue
                try:
                    from ..interface.messaging import (
                        send_slack_file as _ssf, send_whatsapp_media as _swm)
                    if kind == "slack":
                        result = _ssf(msg, recipient, resolved,
                                      thread_ts=thread_ts)
                    else:
                        result = _swm(msg, recipient, resolved)
                    if not result.get("ok"):
                        error = str(result.get("error", "unknown error"))
                        if kind == "slack" and "missing_scope" in error:
                            error = ("Slack app is missing the files:write scope; "
                                     "add it in OAuth & Permissions, then reinstall "
                                     "the app to the workspace")
                        event.send_reply(
                            f"File delivery failed for {resolved.name}: "
                            f"{error}")
                except Exception as exc:
                    event.send_reply(
                        f"File delivery failed for {resolved.name}: {exc}")

        def _surface_reply(text, files=None):
            # Keep transport messages conversational; run IDs remain in
            # traces and diagnostics rather than every chat reply.
            rendered = str(text)
            event.send_reply(rendered)
            # Unified surfaces: a linked surface gets the same
            # result, so WebUI and Slack stay in sync no matter
            # where the run started. WhatsApp only when mirrored
            # (or specified via result delivery, below).
            try:
                from ..interface.surfaces import (
                    conversation_for_slack, slack_for_conversation,
                    whatsapp_mirrored_for_conversation,
                    whatsapp_mirrored_for_slack)
                _cid = str(event.payload.get("conversation_id", "") or "")
                _slack_ch = (slack_for_conversation(config.home, _cid)
                             if _cid else None)
                if _slack_ch and event.source != "slack" and slack_sender is not None:
                    slack_sender(_slack_ch, rendered)
                if event.source == "slack":
                    _linked = conversation_for_slack(
                        config.home, str(event.payload.get("channel", "")))
                    if _linked:
                        try:
                            server._conversations_db().record_exchange(
                                _linked, "assistant", rendered)
                        except Exception:
                            pass
                _wa_mirrored = False
                if _cid:
                    _wa_mirrored = whatsapp_mirrored_for_conversation(config.home, _cid)
                elif event.source == "slack":
                    _wa_mirrored = whatsapp_mirrored_for_slack(
                        config.home, str(event.payload.get("channel", "")))
                if (_wa_mirrored and event.source != "whatsapp"
                        and whatsapp_sender is not None and msg.whatsapp_owner):
                    whatsapp_sender(msg.whatsapp_owner, rendered)
            except Exception:
                pass
            delivery = event.payload.get("result_delivery") or {}
            channel = str(delivery.get("channel", "")).lower()
            if not channel or event.source != "webui":
                return
            if channel == "whatsapp":
                recipient = msg.whatsapp_owner
                result = whatsapp_sender(recipient, rendered) if recipient else {
                    "ok": False, "error": "AIDON_WHATSAPP_OWNER is not configured"}
            elif channel == "slack":
                recipient = msg.slack_outbound_channel
                result = slack_sender(recipient, rendered) if recipient else {
                    "ok": False, "error": "AIDON_SLACK_OUTBOUND_CHANNEL is not configured"}
            else:
                result = {"ok": False, "error": "unsupported result channel"}
            if not result.get("ok"):
                event.send_reply(f"Result delivery to {channel} failed: "
                                 f"{result.get('error', 'unknown error')}")
            else:
                # A delivered result carries its files as bytes
                # too — the phone cannot open a localhost link.
                _deliver_files(files, (channel, recipient))
        # WebUI goals run once. Swarm remains an explicit CLI option;
        # automatically splitting ordinary chat made conversational
        # questions appear multiple times and multiplied model calls.
        if _resume_spec is not None:
            report = runner.run(event.goal, spec=_resume_spec)
        else:
            report = runner.run(event.goal)
        route_callback = event.payload.get("_route_callback")
        if callable(route_callback):
            route_callback(runner.last_route)
        run_control.set_status(run_id, report.status)
        if workspace_store is not None and report.status == "done":
            plan = report.plan
            workflow = {
                "goal": event.goal,
                "summary": getattr(plan, "summary", ""),
                "criteria": list(getattr(plan, "criteria", []) or []),
                "steps": [
                    {"tool": getattr(step, "tool", ""),
                     "description": getattr(step, "description", "")}
                    for step in getattr(plan, "normalized_steps", lambda: [])()
                ],
            }
            workspace_store.publish(run_sandbox.workdir,
                                    report.files or [], workflow)
        # A stopped run hands the frontend its Proceed button
        # through the run record (the poll returns the record),
        # and a finished proceed consumes the stop record so a
        # failed proceed can still be retried.
        if report.status == "stopped" and report.mode_stop:
            try:
                from ..interface.mode_stops import enrich_mode_stop
                enriched = enrich_mode_stop(config.home, report.mode_stop)
            except Exception:
                enriched = dict(report.mode_stop)
            try:
                if run_id in server._web_runs:
                    server._web_runs[run_id]["mode_stop"] = enriched
            except Exception:
                pass
        if _resume_stop_id and report.status == "done":
            try:
                from ..interface.mode_stops import delete_mode_stop
                delete_mode_stop(config.home, _resume_stop_id)
            except Exception:
                pass
        # The mission is over: done always clears the budget
        # record; a failed run clears it only when no stop
        # survives (a failed proceed keeps its stop, so its
        # retry keeps its budget). Stopped runs keep it.
        if _budget_key and (report.status == "done"
                            or (report.status == "failed"
                                and not _resume_stop_id)):
            try:
                from ..capabilities.acquisition_governor import discard_budget_record
                discard_budget_record(config.home, _budget_key)
            except Exception:
                pass
        if report.status == "stopped":
            _stopped = report.mode_stop or {}
            _sid = str(_stopped.get("stop_id", "") or "")
            if _stopped.get("mode") == "ask":
                _shead = "Spec ready — the interview is done."
            else:
                _delivs = _stopped.get("deliverables") or []
                _shead = ("Plan ready" + (f" ({len(_delivs)} deliverable(s): "
                                          + ", ".join(str(d) for d in _delivs[:5])
                                          + ")" if _delivs else "") + ".")
            if event.source == "webui":
                _saction = "Tap Proceed below to continue."
            elif event.source in ("slack", "whatsapp"):
                _saction = "Reply proceed to continue this plan (no ID needed)."
            else:
                _saction = f"Run /proceed {_sid} to continue."
            _surface_reply(f"{_shead}\n{_saction}\n" + format_run_stats(report))
        elif report.status == "chat":
            _surface_reply((report.chat_reply or "Received.") + "\n" + format_run_stats(report))
        else:
            from ..interface.phrasing import (
                phrase_with_model as _pwm, report_text as _report_text)
            _phrase = (_pwm("report-" + report.status, _say)
                       if report.status in ("done", "failed", "rejected",
                                            "cancelled") else None)
            _stats_line = format_run_stats(report)
            _final_text = _report_text(
                report.status, report.files, report.error, run_id,
                "", phrase=_phrase,
                blocked=report.blocked,
                result_text=report.result_text)
            if (event.source == "webui" and report.status == "done"
                    and event.payload.get("skill_source_run_id")):
                from urllib.parse import quote as _quote_skill
                _skill_url = ("/labs?lab=skill&source_run="
                              + _quote_skill(run_id, safe="")
                              + "&conversation_id="
                              + _quote_skill(str(event.conversation_id), safe=""))
                _final_text += ("\n\nThis workflow succeeded. You can refine it "
                                "into a reusable skill in the [Skill Learning Lab]("
                                + _skill_url + ").")
            elif event.source == "webui" and report.status == "done":
                from urllib.parse import quote as _quote_skill
                _skill_url = ("/labs?lab=skill&source_run="
                              + _quote_skill(run_id, safe="")
                              + "&conversation_id="
                              + _quote_skill(str(event.conversation_id), safe=""))
                _final_text += ("\n\nThis workflow succeeded. Want to turn it into a "
                                "reusable skill? [Open Skill Learning Lab]("
                                + _skill_url + ").")
            if event.source == "webui" and report.status == "done" and report.files:
                from pathlib import Path as _ArtifactPath
                _workspace = _ArtifactPath(run_sandbox.workdir).resolve()
                _home = _ArtifactPath(config.home).resolve()
                _links = []
                for _name in report.files:
                    _candidate = (_ArtifactPath(_name) if _ArtifactPath(_name).is_absolute()
                                  else _workspace / str(_name))
                    try:
                        _resolved = _candidate.resolve()
                        if (_resolved.is_file()
                                and _resolved.is_relative_to(_workspace)
                                and _resolved.is_relative_to(_home)):
                            _home_relative = _resolved.relative_to(_home).as_posix()
                            from urllib.parse import quote as _quote_url
                            _url_path = _quote_url(_home_relative, safe="/")
                            _label = _resolved.name
                            if _resolved.suffix.lower() == ".html":
                                _label += " (download)"
                            _links.append(f"- [{_label}](file://{_url_path})")
                    except (OSError, ValueError):
                        continue
                if _links:
                    _final_text += "\n\nFiles in this run's workspace:\n" + "\n".join(_links)
            # The WebUI recognizes the stopwatch line as the start of
            # compact message metadata. Keep all body content (including
            # deliverable links) before it so links remain visible and
            # clickable instead of being escaped into the tiny stats row.
            if _stats_line:
                _final_text += "\n" + _stats_line
            _surface_reply(_final_text, files=report.files)
        # A run that started on Slack or WhatsApp gets its
        # deliverables as uploads on that same surface, after
        # the text report above — localhost links are dead
        # from a phone.
        if report.status == "done" and report.files:
            if event.source == "slack":
                _deliver_files(report.files,
                               ("slack", str(event.payload.get("channel", ""))),
                               thread_ts=str(event.payload.get("thread_ts")
                                             or event.payload.get("slack_ts", "")
                                             or ""))
            elif event.source == "whatsapp":
                _deliver_files(report.files,
                               ("whatsapp", str(event.payload.get("from", ""))))
            elif event.source == "webui":
                # WebUI results go to the linked Slack conversation,
                # or to the configured outbound channel when there is
                # no conversation link. The text result is mirrored in
                # _surface_reply; deliver the actual file bytes too.
                try:
                    from ..interface.surfaces import slack_for_conversation
                    _linked_slack = slack_for_conversation(
                        config.home,
                        str(event.payload.get("conversation_id", "") or ""))
                    _delivery = event.payload.get("result_delivery") or {}
                    _explicit_slack = (
                        str(_delivery.get("channel", "")).lower() == "slack"
                        and str(msg.slack_outbound_channel or ""))
                    _slack_target = (_linked_slack or
                                     msg.slack_outbound_channel or "")
                    if (_slack_target and
                            str(_slack_target) != _explicit_slack):
                        _deliver_files(report.files,
                                       ("slack", str(_slack_target)))
                except Exception:
                    pass

    dispatcher.register("webui", _goal_handler)
    dispatcher.register("whatsapp", _goal_handler)
    dispatcher.register("slack", _goal_handler)
    # Preserve the old ``launch.sh --both`` behavior: the full WebUI
    # can poll Slack when explicitly enabled, while the separate
    # webhook-only process does not start a duplicate poller.
    import os as _slack_os
    _slack_inbox = None
    _slack_stop = threading.Event()
    if msg.slack_bot_token:
        from ..interface.slack_inbox import SlackInbox
        from .config import _env_int as _safe_env_int
        _slack_inbox = SlackInbox(
            config.home, dispatcher, slack_sender,
            ack_after=max(0, _safe_env_int("AIDON_SLACK_ACK_AFTER_SECONDS", 15)),
            recorder=server._record_surface_exchange)
        server.slack_message_dispatch = _slack_inbox.enqueue
    if (not args.webhook_only and _slack_os.environ.get("AIDON_SLACK_AUTOSTART", "0") == "1"
            and msg.slack_bot_token):
        from ..interface.messaging import poll_slack_once as _poll_slack_once
        _channels = [c.strip() for c in _slack_os.environ.get(
            "AIDON_SLACK_INBOUND_CHANNEL", "").split(",") if c.strip()]
        if _channels:
            _poll_seconds = max(3, _safe_env_int("AIDON_SLACK_POLL_SECONDS", 10))
            _cursor_path = config.home / "slack_poll_cursors.json"

            def _slack_poll_loop():
                import time as _slack_time
                from ..interface.slack_polling import load_poll_cursors, save_poll_cursors
                _retry_after = {}
                try:
                    _cursors = load_poll_cursors(_cursor_path, _channels)
                except (OSError, ValueError):
                    print("Slack polling stopped: restore the invalid cursor file before restarting")
                    return
                while not _slack_stop.is_set():
                    for _channel in _channels:
                        if _slack_time.monotonic() < _retry_after.get(_channel, 0):
                            continue
                        try:
                            _messages, _last = _poll_slack_once(
                                msg, _channel,
                                last_ts=str(_cursors.get(_channel, "")))
                            from ..interface.slack_polling import process_polled_messages

                            def _handle_polled_message(_message):
                                server.handle_slack_message(
                                    {"event": {**_message, "type": "message", "channel": _channel}},
                                    dispatch=_slack_inbox.enqueue)

                            def _save_slack_cursor(_timestamp):
                                _cursors[_channel] = _timestamp
                                save_poll_cursors(_cursor_path, _cursors)

                            # Persist goals before acknowledging history. Commands
                            # remain available while the inbox worker runs goals.
                            process_polled_messages(
                                _messages, _handle_polled_message,
                                _save_slack_cursor)
                            # Bot messages are filtered from dispatch but still
                            # advance history. Save that cursor after user work.
                            if _last and _last > str(_cursors.get(_channel, "")):
                                _cursors[_channel] = _last
                                save_poll_cursors(_cursor_path, _cursors)
                            # conversations.history does not include thread
                            # replies. While a stopped plan exists, poll only
                            # its originating thread so "proceed" can resume
                            # from a Slack reply instead of requiring a new
                            # top-level message.
                            from ..interface.mode_stops import (
                                mode_stops_for_conversation)
                            from ..interface.messaging import (
                                poll_slack_thread_once)
                            from decimal import Decimal as _Decimal
                            from datetime import datetime as _datetime
                            for _stop in mode_stops_for_conversation(
                                    config.home, "slack:" + _channel):
                                _thread_ts = str(_stop.get("thread_ts", "") or "")
                                if not _thread_ts:
                                    continue
                                _thread_key = f"thread:{_channel}:{_thread_ts}"
                                _thread_cursor = _cursors.get(_thread_key)
                                if not _thread_cursor:
                                    try:
                                        _created = _datetime.fromisoformat(
                                            str(_stop.get("created_at", "")))
                                        _created_epoch = _created.timestamp()
                                        _thread_cursor = (
                                            f"{int(_created_epoch)}."
                                            f"{int((_created_epoch % 1) * 1_000_000):06d}")
                                    except (TypeError, ValueError, OverflowError):
                                        _thread_cursor = _thread_ts
                                    _cursors[_thread_key] = _thread_cursor
                                    save_poll_cursors(_cursor_path, _cursors)
                                _thread_messages, _thread_last = poll_slack_thread_once(
                                    msg, _channel, _thread_ts,
                                    last_ts=str(_thread_cursor))

                                def _handle_thread_reply(_message):
                                    server.handle_slack_message(
                                        {"event": {**_message, "type": "message",
                                                    "channel": _channel}},
                                        dispatch=_slack_inbox.enqueue)

                                def _save_thread_cursor(_timestamp):
                                    _cursors[_thread_key] = _timestamp
                                    save_poll_cursors(_cursor_path, _cursors)

                                process_polled_messages(
                                    _thread_messages, _handle_thread_reply,
                                    _save_thread_cursor)
                                if (_thread_last and _Decimal(_thread_last)
                                        > _Decimal(str(_cursors.get(_thread_key, "0")))):
                                    _cursors[_thread_key] = _thread_last
                                    save_poll_cursors(_cursor_path, _cursors)
                        except Exception as _exc:
                            print(f"Slack poll error for {_channel}: {_exc}")
                            _retry_after[_channel] = _slack_time.monotonic() + getattr(
                                _exc, "retry_after", _poll_seconds)
                    _slack_stop.wait(_poll_seconds)

            _slack_thread = threading.Thread(
                target=_slack_poll_loop, daemon=True, name="slack-inbound")
            _slack_thread.start()
            print("Slack inbound polling auto-started")
    missing = msg.whatsapp_missing()
    _startup_style("section", "Messaging")
    if missing:
        _startup_style("warn", f"WhatsApp incomplete; missing: {', '.join(missing)}")
    if not msg.whatsapp_owner:
        _startup_style("warn", "WhatsApp inbound disabled: AIDON_WHATSAPP_OWNER is not set (owner-only, fail-closed).")
    if not msg.whatsapp_app_secret:
        _startup_style("warn", "WhatsApp signature verification is off: AIDON_WHATSAPP_APP_SECRET is not set.")
    if not msg.slack_bot_token:
        _startup_style("warn", "Slack sending is not configured: SLACK_BOT_TOKEN is not set.")
    if not msg.slack_signing_secret:
        _startup_style("warn", "Slack webhook requests are rejected: AIDON_SLACK_SIGNING_SECRET is not set (fail-closed).")
    try:
        import importlib.util
        qdrant_available = importlib.util.find_spec("qdrant_client") is not None
    except Exception:
        qdrant_available = False
    if not qdrant_available:
        _startup_style("warn", "Qdrant client is not installed; using persistent SQLite memory fallback.")
    elif os.environ.get("AIDON_QDRANT_URL", "").strip():
        _startup_style("info", "Qdrant remote store configured; connection checked when memory is first used.")
    else:
        _startup_style("info", "Qdrant local store available under the Aidon home.")
    _startup_style("section", "WebUI")
    if args.webhook_only:
        _startup_style("ok", f"Webhook listener: http://{args.host}:{args.port}/ (WhatsApp and Slack only)")
        if args.host != "127.0.0.1":
            _startup_style("warn", "Non-local bind requested; expose this only on a protected network.")
    else:
        _startup_style("ok", f"WebUI: http://{args.host}:{args.port}/")
        _startup_style("info", "Webhooks: /webhook/whatsapp and /webhook/slack")
        if args.host == "127.0.0.1":
            _startup_style("info", "External services need a tunnel; localhost is not publicly reachable.")
        else:
            _startup_style("warn", "Non-local bind requested; expose this only on a protected network and configure WebUI token authentication.")
    slack_sender.start()
    whatsapp_sender.start()
    if _slack_inbox is not None:
        _slack_inbox.start()
    try:
        server.serve(port=args.port, host=args.host)
    finally:
        _slack_stop.set()
        if _slack_inbox is not None:
            _slack_inbox.close()
        slack_sender.close()
        whatsapp_sender.close()
    return 0
