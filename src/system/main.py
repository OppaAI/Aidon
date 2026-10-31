"""Command line for Aidon.

Usage:
  python -m src.main goal "Plan a dinner for two"
  python -m src.main goal --conversation dinner "Plan a dinner for two"

Configuration comes from the environment, or from a .env
file in the current directory or in the Aidon home, loaded
without overriding real environment variables:
  AIDON_HOME      where Aidon keeps its state (default ~/.aidon)
  AIDON_MODEL     model name at the endpoint (default muse-spark)
  AIDON_API_ENDPOINT  endpoint URL for the model API
  AIDON_API_KEY   API key for that endpoint
  AIDON_EMBED_MODEL  embedding model, default google/embeddinggemma-2
  AIDON_EMBEDDER=hash  forces the test embedder

Memory starts blank, by design: new collections, no import
from previous chats. If the real embedder is not
installed, the CLI falls back to HashEmbedder with a
warning on stderr, so a run still works offline, with
non-semantic recall. The demo and submission run on the
real embedder, not on the fallback.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .cli_parser import build_parser
from .cli_webui import run_webui
from ..agentic.loop import GoalRunner, format_run_stats
from .model_client import FakeClient
from ..capabilities.sandbox import Sandbox
from .goal_tools import _goal_extra_tools


from .runtime import (_LazyMemory, _cli_gate, _make_governor, _make_memory,
                     _make_web_researcher, _network_available, _run_sandbox,
                     _run_workspace, _scheduled_gate, _startup_style,
                     make_deep_researcher)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cli:
        args.command = "chat"
    elif not args.command:
        parser.error("a command is required (or use --cli for conversational mode)")
    # This surface is called `run`; keep the spelling while using
    # the new goal runner and its current state/control implementation.
    if args.command == "run":
        args.command = "goal"

    config = Config.load()
    config.ensure_home()
    try:
        from .diagnostics import setup_logging
        setup_logging(config.home)
    except Exception:
        pass
    from .model_client import client_for_config
    from ..agentic.counting import CountingClient
    try:
        client = client_for_config(config)
    except ValueError as exc:
        _startup_style("error", f"Model backend error: {exc}")
        return 2
    if isinstance(client, FakeClient):
        _startup_style("warn", "No model backend configured; using FakeClient. "
                       "Set AIDON_BACKEND, AIDON_API_ENDPOINT, and "
                       "AIDON_API_KEY in .env for a real run.")
    # Count every model call in the workflow, including planning, research,
    # verification, repairs, learning, and the final response.
    client = CountingClient(client)
    # Fast model, different model name, optionally its own endpoint.
    # This is how a local small model works too: set the endpoint to a local
    # native server, AIDON_MODEL to the strong model, and AIDON_FAST_MODEL
    # to the small local model. No Native-specific branch in the loop.
    main_client = client
    fast_client = None
    model_router = None
    if config.fast_model and config.fast_model != config.model:
        from dataclasses import replace
        from ..agentic.router import RoutedClient
        # The fast model may live on its own endpoint (e.g. main on the
        # Meta API, fast Llama on a local server). When a fast endpoint
        # is set, backend is re-inferred from it; otherwise the main
        # backend, endpoint, and key carry over unchanged.
        _fast_endpoint = (getattr(config, "fast_api_endpoint", "") or "").strip()
        _fast_key = getattr(config, "fast_api_key", "") or ""
        fast_config = replace(
            config, model=config.fast_model,
            model_num_ctx=config.fast_num_ctx or config.model_num_ctx,
            api_base=_fast_endpoint or config.api_base,
            api_key=_fast_key or config.api_key,
            backend="" if _fast_endpoint else config.backend)
        fast_client = CountingClient(client_for_config(fast_config))
        fast_phases = {s.strip() for s in config.router_fast_phases.split(",") if s.strip()}
        client = RoutedClient(fast_client, client, fast_phases=fast_phases)
        # ALSO build the phase router: fast phases go to the fast
        # client, everything else to the main client. If building
        # it fails, the RoutedClient above stays the behaviour.
        try:
            from ..system.model_router import RouterClient
            phase_clients = {phase: fast_client for phase in fast_phases}
            model_router = RouterClient(phase_clients, default=main_client)
        except Exception:
            model_router = None
    sandbox = Sandbox(
        config.workdir, config.sandbox_timeout_seconds,
        mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
        fsize_mb=config.sandbox_fsize_mb,
        cpu_seconds=config.sandbox_cpu_seconds,
        backend=config.sandbox_backend,
        allow_network=config.sandbox_network,
        require_isolation=config.sandbox_require_isolation)
    memory = _LazyMemory(config)

    from .cli_inspect import run_inspection_command
    result = run_inspection_command(args, config, sandbox, memory)
    if result is not None:
        return result
    if args.command == "mission":
        from ..capabilities.mission import MissionStore
        import json as _json
        store = MissionStore(config.home)
        if args.action == "list":
            missions_dir = config.home / "missions"
            for path in sorted(missions_dir.glob("*/mission.json")) if missions_dir.exists() else []:
                try:
                    data = _json.loads(path.read_text())
                    print(f"{data.get('conversation_id')}: {data.get('status')}")
                except Exception:
                    continue
            return 0
        if args.action == "show":
            try:
                print(store.get(args.id))
            except FileNotFoundError:
                print(f"No mission {args.id}"); return 2
            return 0
        if args.action == "create":
            try:
                spec = _json.loads(args.spec)
            except Exception:
                spec = {}
            print(store.create(args.id, spec))
            return 0
        if args.action == "discard":
            try:
                mission = store.get(args.id)
            except FileNotFoundError:
                print(f"No mission {args.id}"); return 2
            if mission.get("status") != "cancelled" and mission.get("status") != "rejected":
                # Discard is explicit only for cancelled or rejected drafts; otherwise refuse.
                print(f"Discard refused: mission {args.id} status is {mission.get('status')}, not cancelled or rejected"); return 2
            store.set_status(args.id, "cancelled")
            print(f"Discarded {args.id}")
            return 0
        return 2
    if args.command == "outbox":
        from ..capabilities.outbox import Outbox
        outbox = Outbox(config.home)
        if args.action == "list":
            for item in outbox.list():
                print(f"{item.get('id')} to {item.get('to')} subject {item.get('subject')} status {item.get('status')}")
            return 0
        if args.action == "queue":
            print(outbox.queue(args.to, args.subject, args.body))
            return 0
        if args.action == "drain":
            result = outbox.drain(lambda item: print(f"Would send to {item.get('to')}: {item.get('subject')}") or False)
            print(f"Outbox drain, no real sender configured in CLI, sent={result['sent']} failed={result['failed']} remaining={result['remaining']}, items stay queued until a real sender is supplied")
            return 0
        return 2
    if args.command == "export":
        from ..memory.memory_extras import export_points
        from pathlib import Path as _P
        dest = _P(args.dir) if args.dir else config.home / "export"
        dest.mkdir(parents=True, exist_ok=True)
        out_file = dest / "points.jsonl"
        count = export_points(memory, out_file)
        print(f"Exported {count} memory points to {out_file}")
        if args.registry:
            from ..capabilities.registry import export_registry_markdown
            reg_path = export_registry_markdown(config.home, dest / "registry.md")
            print(f"Exported registry to {reg_path}")
        if args.full:
            from ..capabilities.export_full import export_markdown
            print(f"Full export: {export_markdown(config.home, dest / 'full')}")
        return 0
    if args.command == "schedule":
        from ..interface.triggers import Scheduler, SchedulerJob
        scheduler = Scheduler(config.home)
        if args.action == "list":
            for job in scheduler.jobs:
                print(f"{job.name}: {job.goal} schedule={job.schedule} chat={job.conversation_id}")
            return 0
        if args.action == "show":
            for job in scheduler.jobs:
                if job.name == args.name:
                    print(job); return 0
            print(f"No job {args.name}"); return 2
        if args.action == "add":
            if not args.name or not args.goal:
                print("schedule add needs --name and --goal"); return 2
            try:
                scheduler.add_job(SchedulerJob(name=args.name, goal=args.goal,
                                               schedule=args.schedule,
                                               kind=args.kind,
                                               conversation_id=args.conversation))
            except ValueError as exc:
                print(f"schedule add failed: {exc}"); return 2
            print(f"Added job {args.name}")
            return 0
        if args.action == "remove":
            before = len(scheduler.jobs)
            scheduler.jobs = [j for j in scheduler.jobs if j.name != args.name]
            scheduler.save()
            print(f"Removed {args.name}" if len(scheduler.jobs) < before else f"No job {args.name}")
            return 0 if len(scheduler.jobs) < before else 2
        return 2
    if args.command == "practice":
        from ..interface.practice import (add_schedule, get_schedule,
                                          load_schedules, remove_schedule)
        if args.action == "list":
            for s in load_schedules(config.home):
                goals = "; ".join(g["goal"] for g in s.get("goals", []))
                when = s.get("date") or ",".join(
                    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]
                    for d in s.get("days", [])) or "every day"
                print(f"{s['name']}: {when} {s.get('start')}-{s.get('end')} goals=[{goals}]")
            return 0
        if args.action == "add":
            if not args.name or not args.goal:
                print("practice add needs --name and at least one --goal"); return 2
            goals = []
            for i, g in enumerate(args.goal):
                # "goal || spec" splits like the Practice page; an
                # explicit --spec in the same position wins.
                text, _, inline_spec = g.partition("||")
                spec = (args.spec[i] if i < len(args.spec) else ""
                        ) or inline_spec.strip()
                goals.append({"goal": text.strip(), "spec": spec})
            _padd, _perr = add_schedule(config.home, {
                "name": args.name, "date": args.date,
                "days": [d.strip() for d in args.days.split(",") if d.strip()],
                "start": args.start, "end": args.end, "goals": goals,
                "preapprove": args.preapprove})
            if _perr:
                print(f"practice add failed: {_perr}"); return 2
            print(f"Added practice schedule {args.name}")
            return 0
        if args.action == "remove":
            removed = remove_schedule(config.home, args.name)
            print(f"Removed {args.name}" if removed else f"No practice schedule {args.name}")
            return 0 if removed else 2
        if args.action == "run":
            from datetime import datetime, timedelta
            from ..interface.practice import (get_schedule, record_session,
                                              run_practice_session)
            schedule = get_schedule(config.home, args.name)
            if schedule is None:
                print(f"No practice schedule {args.name}"); return 2
            from ..agentic.budgets import Budget as _PB
            from ..agentic.trace import TraceRecorder as _PT
            from ..agentic.verifier_model import ModelVerifier as _PMV
            from ..capabilities.plugins import PluginRegistry as _PPR
            from ..capabilities.sandbox import Sandbox as _PSB
            from ..interface.practice import (PRACTICE_TOOL_NAMES, PracticeGate,
                                              coerce_preapprove, practice_tools)
            import os as _pos
            import re as _pre
            _pdir = config.home / "practice" / _pre.sub(
                r"[^A-Za-z0-9_-]+", "-", schedule["name"]).strip("-")
            _pdir.mkdir(parents=True, exist_ok=True)
            _psandbox = _PSB(_pdir, config.sandbox_timeout_seconds,
                             mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
                             fsize_mb=config.sandbox_fsize_mb,
                             cpu_seconds=config.sandbox_cpu_seconds,
                             backend=config.sandbox_backend,
                             allow_network=config.sandbox_network,
                             require_isolation=config.sandbox_require_isolation)
            _pplugins = _PPR(config.home); _pplugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _pmb
                _pbackend = _pmb(config.execution_backend, _psandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _pbackend = None
            if coerce_preapprove(schedule.get("preapprove", True)):
                from ..capabilities.execution import backend_is_sandboxed
                _pgate = PracticeGate(
                    _pdir, sandboxed=backend_is_sandboxed(_pbackend, _psandbox))
            else:
                _pgate = _cli_gate(config.home, config.approval_timeout_seconds)
            from ..interface.messaging import MessagingConfig as _PMC
            _pmcfg = _PMC.from_env()
            _prunner = GoalRunner(client, _psandbox, _pgate, config.home, memory=memory, conversation_id="practice:" + schedule["name"], plugins=_pplugins, execution_backend=_pbackend, budget=_PB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_PT(config.home, conversation_id="practice:" + schedule["name"]), model_verifier=_PMV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_pbackend, _psandbox),
                                  extra_tools=practice_tools(
                                      _pdir, _pmcfg, owner_email=config.owner_email,
                                      gmail_client_id=_pos.environ.get("GMAIL_CLIENT_ID", ""),
                                      gmail_client_secret=_pos.environ.get("GMAIL_CLIENT_SECRET", ""),
                                      gmail_refresh_token=_pos.environ.get("GMAIL_REFRESH_TOKEN", "")),
                                  allowed_side_effect_tools=set(PRACTICE_TOOL_NAMES))

            def _p_run_goal(goal_text, spec):
                _report = _prunner.run(goal_text, spec=spec)
                return {"status": _report.status, "files": list(_report.files or []),
                        "error": _report.error or "",
                        "elapsed_seconds": getattr(_report, "elapsed_seconds", 0),
                        "tokens": _report.model_tokens_estimated,
                        "run_id": getattr(_prunner, "run_id", "") or "",
                        "dag": _report.dag_summary or {}}

            def _p_deliver(text, goal, result):
                print(text)
                try:
                    from ..interface.messaging import (MessagingConfig as _MC,
                                                        send_slack as _ss,
                                                        send_whatsapp as _sw)
                    _mcfg = _MC.from_env()
                    if _mcfg.slack_outbound_channel:
                        _ss(_mcfg, _mcfg.slack_outbound_channel, text)
                    if _mcfg.whatsapp_owner:
                        _sw(_mcfg, _mcfg.whatsapp_owner, text)
                except Exception as exc:
                    print(f"Practice report delivery failed: {exc}")

            summary = run_practice_session(schedule, _p_run_goal,
                                           datetime.now() + timedelta(minutes=60),
                                           home=config.home, on_report=_p_deliver,
                                           on_progress=lambda live: print(
                                               f"Practice live: {live['goal']} "
                                               f"(attempt {live['attempt']})")
                                           if live else None,
                                           preapprove=coerce_preapprove(
                                               schedule.get("preapprove", True)))
            if not summary["passed"]:
                summary["report"] = (
                    f"Practice session {schedule['name']}: no pass in this window "
                    f"({len(summary['attempts'])} attempts).")
            record_session(config.home, schedule["name"], summary)
            print(f"Practice session {schedule['name']}: "
                  f"{'passed: ' + summary['goal'] if summary['passed'] else 'no pass'} "
                  f"({len(summary['attempts'])} attempts)")
            return 0
        return 2
    if args.command == "sandbox":
        from ..capabilities.execution import make_backend
        try:
            backend = make_backend(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network, vm_host=config.vm_host, vm_user=config.vm_user or None, vm_sudo_password=config.vm_sudo_password or None, vm_use_sudo=config.vm_use_sudo, modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                    modal_memory_mb=config.modal_memory_mb)
            print(f"Sandbox backend: {config.execution_backend}, workdir: {sandbox.workdir}, backend: {backend}")
            return 0
        except Exception as exc:
            print(f"Sandbox check failed, fail closed: {exc}"); return 2
    if args.command == "chain":
        from ..capabilities.chain import load_chain_spec, run_chain, validate_chain_spec
        from ..capabilities.tools import ToolRegistry
        try:
            spec = load_chain_spec(args.path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"Chain spec error: {exc}")
            return 2
        if args.action == "validate":
            failures = validate_chain_spec(spec)
            print(f"Chain valid: {not failures}, failures: {failures}")
            return 0 if not failures else 2
        if args.action == "run":
            result = run_chain(spec, ToolRegistry(sandbox.workdir))
            print(result)
            return 0 if result.get("ok") else 2
        return 2
    if args.command == "chat":
        from ..interface.repl import Repl
        from .model_client import Message
        from ..agentic.think_effort import sampling_for
        messages = []

        def _chat_handler(text: str) -> str:
            messages.append(Message("user", text))
            try:
                reply = client.chat(messages, sampling=sampling_for("respond"))
            except TypeError:
                reply = client.chat(messages)
            messages.append(Message("assistant", str(reply)))
            return str(reply)

        def _remember(text: str) -> str:
            point = memory.remember(text, source="cli-command")
            return f"Remembered in long-term memory: {text} (id {point.id})"

        Repl(_chat_handler, title="Aidon Chat", on_clear=messages.clear,
             on_remember=_remember).run()
        return 0
    if args.command == "computer":
        from ..capabilities.computer_use import capture_screenshot, run_computer_use
        import json as _json
        if args.action == "screenshot":
            result = capture_screenshot(args.path)
            print(result)
            return 0 if result.get("ok") else 2
        if args.action == "run":
            from pathlib import Path
            actions = _json.loads(Path(args.path).read_text(encoding="utf-8"))
            if not isinstance(actions, list):
                actions = actions.get("actions", [])
            result = run_computer_use(actions, approve=lambda kind, detail: args.approve)
            print(result)
            return 0 if result.get("ok") else 2
        return 2

    if args.command == "report":
        from ..interface.report import autonomy_report, daily_report
        if args.action == "daily":
            print(daily_report(config.home))
        else:
            print(autonomy_report(config.home))
        return 0
    if args.command == "demo":
        if args.action == "learn":
            # Real counts, not canned benchmark numbers: experience points and last run.
            try:
                points = memory.list_points("experience", limit=1000)
                print(f"Experience points: {len(points)}")
            except Exception as exc:
                print(f"Experience points: error {exc}")
            from pathlib import Path as _P
            last = _P(config.home) / "learned" / "last_run.json"
            print(f"Last run file: {last} exists={last.is_file()}")
            if last.is_file():
                print(last.read_text()[:500])
            return 0
        if args.action == "invent-tool":
            if not args.name:
                print("demo invent-tool needs --name"); return 2
            from pathlib import Path as _P
            from ..capabilities.plugins import scan_plugin
            draft_dir = _P(config.home) / "plugins_draft" / args.name
            draft_dir.mkdir(parents=True, exist_ok=True)
            (draft_dir / "tool.py").write_text(f'"""Draft plugin {args.name}, generated by demo invent-tool, not adopted yet."""\n\ndef {args.name}_tool(text: str) -> str:\n    return text\n', encoding="utf-8")
            (draft_dir / "skill.md").write_text(f"# {args.name}\n\n{args.description or 'Draft skill, adoption still needs the gate.'}\n", encoding="utf-8")
            (draft_dir / "manifest.json").write_text(f'{{"name": "{args.name}", "version": "0.1.0", "description": "{args.description or "draft"}"}}', encoding="utf-8")
            findings = scan_plugin(draft_dir)
            print(f"Draft plugin at {draft_dir}, scan findings: {len(findings)}")
            for finding in findings:
                print(f"  {finding.type} {finding.path}:{finding.line} {finding.message}")
            print("Not adopted, adoption still needs the plugin gate and your approval.")
            return 0
        return 2
    if args.command == "slack":
        from ..interface.messaging import MessagingConfig, poll_slack_once
        import os as _os
        cfg = MessagingConfig.from_env()
        if args.action == "status":
            print(f"Slack configured: {cfg.slack_configured()}, channel env set: {bool(_os.environ.get('AIDON_SLACK_INBOUND_CHANNEL'))}")
            return 0
        if args.action == "listen":
            if not cfg.slack_configured():
                print("Slack not configured, no token, nothing polled"); return 2
            channel = args.channel or _os.environ.get("AIDON_SLACK_INBOUND_CHANNEL", "")
            if not channel:
                print("Slack listen needs --channel or AIDON_SLACK_INBOUND_CHANNEL"); return 2
            import time as _time
            last_ts = ""
            end = _time.time() + args.seconds if args.seconds else _time.time()
            while True:
                messages, last_ts = poll_slack_once(cfg, channel, last_ts=last_ts)
                for message in messages:
                    print(f"Slack message: {message.get('text', '')[:200]}")
                    if args.run:
                        from ..agentic.budgets import Budget as _SB
                        from ..agentic.trace import TraceRecorder as _ST
                        from ..agentic.verifier_model import ModelVerifier as _SMV
                        from ..capabilities.plugins import PluginRegistry as _SPR
                        _splugins = _SPR(config.home); _splugins.load_all()
                        try:
                            from ..capabilities.execution import make_backend as _smb
                            _sbackend = _smb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
                        except Exception:
                            _sbackend = None
                        _srunner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id="slack:" + channel, plugins=_splugins, execution_backend=_sbackend, budget=_SB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_ST(config.home, conversation_id="slack:" + channel), model_verifier=_SMV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_sbackend, sandbox))
                        _sreport = _srunner.run(message.get("text", ""))
                        print(f"Slack run: {_sreport.status}, files: {', '.join(_sreport.files)}")
                if not args.seconds or _time.time() >= end:
                    break
                _time.sleep(10)
            return 0
        return 2
    if args.command == "whatsapp":
        from ..interface.messaging import MessagingConfig
        from ..interface.whatsapp import send_whatsapp_message
        cfg = MessagingConfig.from_env()
        if args.action == "status":
            print(f"WhatsApp configured: {cfg.whatsapp_configured()}, missing: {cfg.whatsapp_missing()}")
            return 0
        if args.action == "send":
            if not args.to or not args.text:
                print("whatsapp send needs --to and --text"); return 2
            result = send_whatsapp_message(cfg, args.to, args.text)
            print(result)
            return 0 if result.get("ok") else 2
        return 2
    if args.command == "tick":
        from datetime import datetime
        from ..interface.triggers import Scheduler
        scheduler = Scheduler(config.home)
        due = scheduler.check_due(datetime.now())
        if not due:
            print("Tick: no jobs due")
            return 0
        for job in due:
            print(f"Tick due job: {job.name}: {job.goal}")
            # Run via GoalRunner, same construction as scheduler serve, without semantic router.
            from ..agentic.budgets import Budget as _B
            from ..agentic.trace import TraceRecorder as _T
            from ..agentic.verifier_model import ModelVerifier as _MV
            from ..capabilities.plugins import PluginRegistry as _PR
            _plugins = _PR(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb
                _backend = _mb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            from ..interface.run_control import RunControl as _TickControl
            import uuid as _tick_uuid
            _tick_run_id = _tick_uuid.uuid4().hex[:12]
            _tick_control = _TickControl(config.home)
            _tick_session = getattr(job, "conversation_id", "") or getattr(job, "project_id", "") or "scheduled"
            _tick_control.register(_tick_run_id, job.goal, _tick_session)
            print(f"Tick job {job.name}: run id {_tick_run_id}")
            runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds, _tick_session), config.home, memory=memory, conversation_id=_tick_session, plugins=_plugins, execution_backend=_backend, budget=_B(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T(config.home, conversation_id=_tick_session), model_verifier=_MV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox), run_id=_tick_run_id, run_control=_tick_control)
            report = runner.run(job.goal)
            print(f"Tick job {job.name}: {report.status}, files: {', '.join(report.files)}")
        return 0
    if args.command == "repl":
        from ..interface.repl import Repl
        _cli_think_state = {"on": False}
        _cli_research_state = {"on": False}
        _cli_mode_state = {"mode": "plan"}

        def _repl_handler(goal: str) -> str:
            from ..agentic.budgets import Budget as _B2
            from ..agentic.trace import TraceRecorder as _T2
            from ..agentic.verifier_model import ModelVerifier as _MV2
            from ..capabilities.plugins import PluginRegistry as _PR2
            _plugins = _PR2(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb2
                _backend = _mb2(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            from ..interface.surfaces import parse_think_command as _cli_think
            from ..interface.surfaces import parse_research_command as _cli_research
            _cli_cmd = _cli_think(goal)
            _cli_once = False
            if _cli_cmd is not None and _cli_cmd["action"] == "once":
                goal = _cli_cmd["goal"]
                _cli_once = True
            _cli_rcmd = _cli_research(goal)
            _cli_research_once = False
            if _cli_rcmd is not None:
                if _cli_rcmd["action"] == "toggle":
                    _cli_research_state["on"] = not _cli_research_state["on"]
                    return (f"Deep-research mode {'on' if _cli_research_state['on'] else 'off'} "
                            "for this REPL session.")
                goal = _cli_rcmd["goal"]
                _cli_research_once = True
            from ..interface.surfaces import parse_mode_command as _cli_mode
            from ..interface.surfaces import parse_proceed_command as _cli_proceed
            _cli_mode_once = None
            _cli_mcmd = _cli_mode(goal)
            if _cli_mcmd is not None:
                if _cli_mcmd["action"] == "set":
                    _cli_mode_state["mode"] = _cli_mcmd["mode"]
                    if _cli_mcmd["mode"] == "execute":
                        return "Execute mode on for this REPL session — goals run directly."
                    return f"{_cli_mcmd['mode'].capitalize()} mode on for this REPL session."
                goal = _cli_mcmd["goal"]
                _cli_mode_once = _cli_mcmd["mode"]
            _cli_resume_plan = None
            _cli_resume_spec = None
            _cli_resume_stop_id = ""
            _cli_pcmd = _cli_proceed(goal)
            if _cli_pcmd is not None:
                from ..interface.mode_stops import load_mode_stop as _cli_load_stop
                from ..interface.mode_stops import latest_mode_stop as _cli_latest_stop
                _stop = (_cli_load_stop(config.home, _cli_pcmd["stop_id"])
                         if _cli_pcmd["stop_id"]
                         else _cli_latest_stop(config.home, "cli"))
                if _stop is None:
                    if _cli_pcmd["stop_id"]:
                        return f"No stopped run with id {_cli_pcmd['stop_id']}."
                    return "No stopped run to proceed with."
                goal = _stop["goal"]
                _cli_mode_once = "execute"
                _cli_resume_stop_id = _stop["stop_id"]
                if _stop["mode"] == "plan" and isinstance(_stop.get("plan"), dict) and _stop["plan"].get("steps"):
                    from ..agentic.loop import Plan as _CliPlan
                    _rp = _stop["plan"]
                    _cli_resume_plan = _CliPlan(
                        steps=_rp.get("steps", []),
                        deliverables=[str(d) for d in _rp.get("deliverables", [])],
                        criteria=[str(c) for c in _rp.get("criteria", [])])
                elif _stop["mode"] == "ask" and isinstance(_stop.get("spec"), dict):
                    from ..agentic.interview import Spec as _CliSpec
                    _cli_resume_spec = _CliSpec.from_dict(_stop["spec"])
            try:
                from ..agentic.clef_router import build_router as _cli_build_router
                _cli_router = _cli_build_router(
                    config, memory.embedder,
                    config.home / "semantic_router_vectors.json")
            except Exception:
                _cli_router = None
            _cli_clef_client = None
            try:
                if getattr(config, "clef_url", ""):
                    from ..agentic.clef_client import ClefClient as _CliClefClient
                    _cli_clef_client = _CliClefClient(
                        config.clef_url,
                        model=getattr(config, "clef_model", "") or "Cloudflare/clef-flash",
                        key=getattr(config, "clef_key", "") or "",
                        secret=getattr(config, "clef_secret", "") or "")
            except Exception:
                _cli_clef_client = None
            runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id="cli", plugins=_plugins, execution_backend=_backend, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, conversation_id="cli"), model_verifier=_MV2(client), kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox), think_max=_cli_once or _cli_think_state["on"], research_mode=_cli_research_once or _cli_research_state["on"], deep_researcher=make_deep_researcher(config), mode=_cli_mode_once or _cli_mode_state["mode"], question_ask=lambda q: input(f"\nAidon: {q}\nYou: "), conversation_key="cli", resume_plan=_cli_resume_plan, semantic_router=_cli_router, clef_client=_cli_clef_client)
            if _cli_resume_spec is not None:
                report = runner.run(goal, spec=_cli_resume_spec)
            else:
                report = runner.run(goal)
            if _cli_resume_stop_id and report.status == "done":
                try:
                    from ..interface.mode_stops import delete_mode_stop as _cli_del_stop
                    _cli_del_stop(config.home, _cli_resume_stop_id)
                except Exception:
                    pass
            if report.status == "chat":
                return report.chat_reply or "Received."
            if report.status == "stopped":
                _sid = (report.mode_stop or {}).get("stop_id", "")
                _smode = (report.mode_stop or {}).get("mode", "") or "run"
                return f"[stopped] {_smode} ready — run /proceed {_sid} to continue."
            return f"[{report.status}] {', '.join(report.files)} {report.error}".strip()
        def _remember(text: str) -> str:
            point = memory.remember(text, source="cli-command")
            return f"Remembered in long-term memory: {text} (id {point.id})"

        def _on_think() -> str:
            _cli_think_state["on"] = not _cli_think_state["on"]
            return ("Maximum thinking on for this REPL session."
                    if _cli_think_state["on"]
                    else "Maximum thinking off for this REPL session.")

        Repl(_repl_handler, on_remember=_remember, on_think=_on_think).run()
        return 0
    if args.command == "webui":
        return run_webui(args, config, memory)
    if args.command == "v1":
        from ..interface.v1 import AuthStore, GoalStore, handle
        # Live v1 and MCP execute callback, wired here, not 501:
        # a v1 goal runs through the same GoalRunner as a CLI goal,
        # with the same plugins, budget, trace, verifier, and backend.
        # Approval is asked on the server console, via _cli_gate,
        # so a v1 run does not auto-approve live execution.
        def _v1_execute(goal_record):
            from ..agentic.budgets import Budget as _VB
            from ..agentic.trace import TraceRecorder as _VT
            from ..agentic.verifier_model import ModelVerifier as _VM
            from ..capabilities.plugins import PluginRegistry as _VP
            _plugins = _VP(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _vmb
                _backend = _vmb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            _runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home,
                                 memory=memory, conversation_id=goal_record.conversation_id, plugins=_plugins,
                                 execution_backend=_backend, budget=_VB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                 trace=_VT(config.home, conversation_id=goal_record.conversation_id), model_verifier=_VM(client),
                                 kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox))
            _report = _runner.run(goal_record.goal)
            return {"status": _report.status, "files": _report.files, "error": _report.error}
        auth = AuthStore(config.home)
        goals = GoalStore(config.home)
        if args.action == "revoke":
            ok = auth.revoke(args.name)
            print(f"Revoked {args.name}" if ok else f"No such key {args.name}")
            return 0 if ok else 2
        if args.action == "keygen":
            key, record = auth.create_key(args.name, args.scope)
            print("Save this key now, it is shown once, only its hash is stored:")
            print(key)
            print(f"Name: {record.name}, scope: {record.scope}, hash prefix: {record.key_hash[:12]}")
            return 0
        if args.action == "mcp":
            from ..interface.mcp_server import MCPServer
            MCPServer(auth, goals, execute=_v1_execute).run_stdio()
            return 0
        if args.action == "mcp-http":
            # Streamable HTTP MCP: JSON-RPC over HTTP at /mcp,
            # Bearer token, same store and scopes as stdio MCP.
            from ..interface.mcp_server import MCPServer
            import json as _json2
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            mcp_server = MCPServer(auth, goals, execute=_v1_execute)

            class _McpHandler(BaseHTTPRequestHandler):
                def do_POST(self):
                    if self.path != "/mcp":
                        self.send_response(404); self.end_headers(); return
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length) if length else b""
                    try:
                        body = _json2.loads(raw) if raw else {}
                    except Exception:
                        body = {}
                    auth_header = self.headers.get("Authorization", "")
                    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
                    payload = mcp_server.handle(body, token)
                    data = _json2.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args):
                    pass

            print(f"Streamable HTTP MCP on http://127.0.0.1:{args.port}/mcp, localhost only, Bearer <redacted> required except initialize and ping")
            ThreadingHTTPServer(("127.0.0.1", args.port), _McpHandler).serve_forever()
            return 0
        # serve: localhost only, stdlib HTTP, v1 handler only.
        import json as _json
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def _do(self, method):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = _json.loads(raw) if raw else {}
                except Exception:
                    body = {}
                status, payload = handle(auth, goals, method, self.path,
                                         dict(self.headers), body, execute=_v1_execute)
                data = _json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def log_message(self, *args):
                pass

        print(f"v1 API on http://127.0.0.1:{args.port}/api/v1/, localhost only, Bearer token required except health")
        HTTPServer(("127.0.0.1", args.port), _Handler).serve_forever()
        return 0
    if args.command == "trigger":
        from datetime import datetime
        from ..interface.triggers import (ALLOWED_SOURCES, IngestWatcher, Scheduler,
                               TriggerDispatcher, handle_webhook)
        if args.action == "list":
            print("Trigger sources, all start a DAG run, none bypass approval:")
            for source in ALLOWED_SOURCES:
                print(f"  {source}")
            print("  No Telegram source, by owner decision.")
            return 0
        if args.action == "ingest":
            watcher = IngestWatcher(config.ingest_path, config.home)
            for event in watcher.events(conversation_id=args.conversation):
                print(f"ingest trigger: {event.payload['path']}")
            return 0
        if args.action == "schedule":
            scheduler = Scheduler(config.home)
            for job in scheduler.check_due(datetime.now()):
                print(f"scheduled trigger: {job.name}: {job.goal}")
            return 0
        if args.action == "webhook":
            if not args.name:
                print("trigger webhook needs a name")
                return 2
            event = handle_webhook(args.name, {"goal": args.goal},
                                   conversation_id=args.conversation)
            dispatcher = TriggerDispatcher()
            dispatcher.register("webhook", lambda e: print(f"webhook trigger: {e.kind}: {e.goal}"))
            dispatcher.dispatch(event)
            return 0
        return 2
    if args.command == "unity":
        from ..capabilities.unity import UnityAssetBuilder
        builder = UnityAssetBuilder(config.home / "unity")
        if args.action == "asset":
            result = builder.generate_asset(args.name, shape=args.shape, color=args.color)
            print(f"Unity asset: {result['name']}, shape {result['shape']}")
            print(f"OBJ: {result['obj']}")
            print(f"C# script: {result['script']}")
            print(f"Import: open the folder in Unity, the OBJ imports as a mesh, the C# script is a MonoBehaviour that colours it {args.color}.")
            print(f"Build command, if Unity is installed: {builder.build_command(result['project_dir'])}")
            return 0
        print(builder.build_command(config.home / "unity" / args.name))
        return 0
    if args.command == "image":
        from ..capabilities.image_gen import generate_image, edit_image, save_image
        from pathlib import Path as _Path
        if not config.api_key or not config.api_base:
            print("Image generation needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark, no separate image key.")
            return 2
        out = _Path(args.output) if args.output else config.home / "library" / "files" / "image.png"
        if args.action == "generate":
            data = generate_image(args.prompt, config.api_key, config.api_base, size=args.size, model=config.image_model)
        else:
            data = edit_image(args.prompt, config.api_key, config.api_base, reference_images=args.refs or [], size=args.size, model=config.image_model)
        path = save_image(data, out)
        print(f"Image saved: {path}, model {config.image_model}")
        return 0
    if args.command == "video":
        from ..capabilities.video import edit_video, extract_frames, encode_frames, ffmpeg_available
        if not ffmpeg_available():
            print("ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake video is produced.")
            return 2
        if args.action == "extract":
            frames = extract_frames(args.input, args.output, fps=args.fps)
            print(f"Extracted {len(frames)} frames to {args.output}")
            return 0
        if args.action == "encode":
            from pathlib import Path as _P
            frames = sorted(_P(args.input).glob("frame*.jpg"))
            out = encode_frames(frames, args.output, fps=args.fps)
            print(f"Encoded video: {out}")
            return 0
        if not args.prompt:
            print("video edit needs --prompt, a noun phrase for SAM, for example --prompt 'red toolbox'")
            return 2
        if not config.api_key or not config.api_base:
            print("video edit needs SAM via AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient
        sam_client = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = edit_video(args.input, args.output, args.prompt, sam_client, fps=args.fps)
        if "error" in result:
            print(f"Video pipeline error: {result['error']}")
            return 1
        print(f"Video edited: {result['output']}, frames: {len(result['frames'])}, prompt: {result['prompt']}")
        print("Edited video uses SAM box overlays, not pixel-mask compositing, mask decoding needs the optional meta-sam-parser package, see src/video.py.")
        return 0
    if args.command == "sam":
        if not config.api_key or not config.api_base:
            print("SAM needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient, build_object_prompt
        client_sam = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = client_sam.segment(args.images, build_object_prompt(args.prompt), propagation=args.propagation)
        if "error" in result:
            print(f"SAM error: {result['error']}")
            return 1
        for seg in result["segments"]:
            print(f"frame {seg['frame']} id {seg['object_id']} box {seg['box']} confidence {seg['confidence']}")
        print("SAM is the segment and track step only. Frame extraction and video re-encoding need ffmpeg, not built here.")
        return 0
    if args.command == "control":
        from ..interface.run_control import RunControl
        control = RunControl(config.home)
        if args.action == "list":
            for run in control.list_runs():
                print(f"{run['run_id']} {run['status']} {run['goal']} steer_notes={len(run.get('steer_notes', []))}")
            return 0
        if args.action == "cancel":
            ok = control.cancel(args.run_id) if args.run_id else False
            print(f"Cancelled {args.run_id}" if ok else f"No such run {args.run_id}")
            return 0 if ok else 2
        if args.action == "steer":
            ok = control.steer(args.run_id, args.note or "") if args.run_id else False
            print(f"Steered {args.run_id}: {args.note}" if ok else f"No such run {args.run_id}")
            return 0 if ok else 2
    if args.command == "scheduler":
        from ..interface.preapproval import (DEFAULT_HOURS, HELP_TEXT,
                                             IRREVERSIBLE, MAX_HOURS,
                                             PreapprovalStore, grantable_kind,
                                             kind_from_token)
        import time as _time
        store = PreapprovalStore(config.home)
        if args.action == "preapprove":
            # Same kinds and same refusal as the WebUI, WhatsApp, and
            # Slack surfaces: by name or by the menu number.
            raw_kind = args.kind or "sandbox"
            kind = kind_from_token(raw_kind)
            if kind is None:
                print(f"Unknown kind {raw_kind!r}.\n\n{HELP_TEXT}")
                return 2
            if kind not in IRREVERSIBLE and not grantable_kind(kind):
                print(f"Unknown kind {kind!r}.\n\n{HELP_TEXT}")
                return 2
            hours = args.hours if args.hours and args.hours > 0 else DEFAULT_HOURS
            hours = min(hours, MAX_HOURS)
            ok = store.grant(args.spec, kind,
                             _time.time() + hours * 3600, reversible=True)
            if ok:
                print(f"Pre-approved {kind} for {args.spec}, for the next {hours:g}h.")
                print("Irreversible steps (send, publish, spend, delete, install) still ask.")
                return 0
            print(f"Refused: {kind} can never be pre-approved, it is irreversible "
                  f"or leaves the machine.")
            return 2
        if args.action == "list":
            rows = store.active()
            if not rows:
                print("No pre-approvals are active.")
                return 0
            now = _time.time()
            for row in rows:
                left = max(0.0, (float(row["until"]) - now) / 3600.0)
                spec_id = row["spec_id"]
                kind_name = row["action_kind"]
                print(f"{spec_id} {kind_name} {left:.1f}h left")
            return 0
        if args.action == "revoke":
            kind = None
            if args.kind and args.kind.lower() not in ("all", "*"):
                kind = kind_from_token(args.kind)
                if kind is None:
                    print(f"Unknown kind {args.kind!r}.\n\n{HELP_TEXT}")
                    return 2
            removed = store.revoke(args.spec, kind)
            print(f"Revoked {removed} pre-approval(s) for {args.spec}."
                  if removed else f"Nothing was pre-approved for {args.spec}.")
            return 0 if removed else 2
        if args.action == "morning":
            for item in store.morning_report():
                print(f"{item['spec_id']} {item['action_kind']}: undo: {item['undo']}")
            return 0
        # serve: poll due jobs every 60 seconds, run each via GoalRunner.
        # KNOWN BUG, documented, not fixed (decision 2026-10-28: keep
        # the live timer/WebUI path, fix this post-hackathon): this
        # branch references server and msg, which are only assigned
        # in the webui serve branch, so they are unbound locals
        # here. The branch crashes at startup on
        # server.practice_session_runner, and the practice-runner
        # and practice-delivery uses would crash or silently drop
        # after it. Do not run schedule serve until the WebUI
        # construction is shared with this branch (see ROADMAP).
        from datetime import datetime
        from ..interface.triggers import Scheduler
        print("Scheduler daemon, polling every 60 seconds, Ctrl-C to stop.")
        print("Scheduler daemon also watches ingest and polls Slack, when configured, same loop.")
        def _full_scheduler_runner(session: str = ""):
            from ..agentic.budgets import Budget as _B2
            from ..agentic.trace import TraceRecorder as _T2
            from ..agentic.verifier_model import ModelVerifier as _M2
            from ..capabilities.plugins import PluginRegistry as _P2
            _pl = _P2(config.home); _pl.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb2
                _be = _mb2(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _be = None
            return GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id=session, plugins=_pl, execution_backend=_be, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, conversation_id=session), model_verifier=_M2(client), kernel_mode=config.kernel_mode, network_available=_network_available(_be, sandbox))

        import os as _os
        _slack_last_ts = ""
        _slack_ts_path = config.home / "slack_last_ts.txt"
        if _slack_ts_path.is_file():
            _slack_last_ts = _slack_ts_path.read_text().strip()
        # Practice sessions: the serve daemon owns the window tick
        # and the report delivery. Each schedule gets its own
        # practice folder in the sandbox, and a runner built for
        # it: core tools live in that folder, the gate is the
        # PracticeGate for pre-approved schedules (the WebUI
        # gate otherwise, so it stops for approval), and the
        # only extra tools are the owner-only practice tools.
        def _practice_runner(schedule):
            from ..agentic.budgets import Budget as _B3
            from ..agentic.trace import TraceRecorder as _T3
            from ..agentic.verifier_model import ModelVerifier as _M3
            from ..capabilities.plugins import PluginRegistry as _P3
            from ..capabilities.sandbox import Sandbox as _S3
            from ..interface.practice import (PRACTICE_TOOL_NAMES, PracticeGate,
                                              coerce_preapprove, practice_tools)
            import os as _os3
            practice_dir = config.home / "practice" / server._safe_name(schedule["name"])
            practice_dir.mkdir(parents=True, exist_ok=True)
            _psandbox = _S3(practice_dir, config.sandbox_timeout_seconds,
                            mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
                            fsize_mb=config.sandbox_fsize_mb,
                            cpu_seconds=config.sandbox_cpu_seconds,
                            backend=config.sandbox_backend,
                            allow_network=config.sandbox_network,
                            require_isolation=config.sandbox_require_isolation)
            _pl = _P3(config.home); _pl.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb3
                _be = _mb3(config.execution_backend, _psandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _be = None
            if coerce_preapprove(schedule.get("preapprove", True)):
                from ..capabilities.execution import backend_is_sandboxed
                _gate = PracticeGate(
                    practice_dir, sandboxed=backend_is_sandboxed(_be, _psandbox))
            else:
                _gate = server.webui_gate.gate
            return GoalRunner(client, _psandbox, _gate, config.home, memory=memory,
                              conversation_id="practice:" + schedule["name"], plugins=_pl, execution_backend=_be,
                              budget=_B3(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                              trace=_T3(config.home, conversation_id="practice:" + schedule["name"]), model_verifier=_M3(client),
                              kernel_mode=config.kernel_mode,
                              network_available=_network_available(_be, _psandbox),
                              extra_tools=practice_tools(
                                  practice_dir, msg, owner_email=config.owner_email,
                                  gmail_client_id=_os3.environ.get("GMAIL_CLIENT_ID", ""),
                                  gmail_client_secret=_os3.environ.get("GMAIL_CLIENT_SECRET", ""),
                                  gmail_refresh_token=_os3.environ.get("GMAIL_REFRESH_TOKEN", "")),
                              allowed_side_effect_tools=set(PRACTICE_TOOL_NAMES))

        def _practice_deliver(text, goal, result):
            # Practice reports are isolated to the Practice
            # page (the session record) plus the owner's
            # specified surfaces below. They are deliberately
            # NOT recorded into any chat conversation, so a
            # practice run never shows up among real runs.
            try:
                if msg.slack_outbound_channel:
                    slack_sender(msg.slack_outbound_channel, text)
            except Exception:
                pass
            # Practice reports are explicitly specified outputs,
            # so WhatsApp delivery here follows the owner's rule.
            try:
                if msg.whatsapp_owner:
                    whatsapp_sender(msg.whatsapp_owner, text)
            except Exception:
                pass

        def _practice_session_sync(schedule, end):
            from ..interface.practice import (coerce_preapprove,
                                              run_practice_session as _rps,
                                              record_session as _rec,
                                              update_live as _live)

            def _run_goal(goal_text, spec):
                _runner = _practice_runner(schedule)
                _report = _runner.run(goal_text, spec=spec)
                return {"status": _report.status, "files": list(_report.files or []),
                        "error": _report.error or "",
                        "elapsed_seconds": getattr(_report, "elapsed_seconds", 0),
                        "tokens": _report.model_tokens_estimated,
                        "run_id": getattr(_runner, "run_id", "") or "",
                        "dag": _report.dag_summary or {}}

            def _on_progress(live):
                try:
                    _live(config.home, schedule["name"], live)
                except Exception:
                    pass

            summary = _rps(schedule, _run_goal, end, home=config.home,
                           on_report=_practice_deliver, on_progress=_on_progress,
                           preapprove=coerce_preapprove(schedule.get("preapprove", True)))
            if not summary["passed"]:
                # Window closed with no pass: the no-pass note
                # lives in the session record on the Practice
                # page, so a silent failure never looks like
                # a pass — and never lands in a chat conversation.
                summary["report"] = (
                    f"Practice session {schedule['name']}: no pass in this window "
                    f"({len(summary['attempts'])} attempts).")
            _rec(config.home, schedule["name"], summary)
            print(f"Practice session {schedule['name']}: "
                  f"{'passed' if summary['passed'] else 'no pass'}")
        server.practice_session_runner = _practice_session_sync
        while True:
            scheduler = Scheduler(config.home)
            # Tick wiring: SchedulerLoop carries the outbox drain
            # and the scheduled-run notify hook for this daemon.
            # Sending is not configured here, so the outbox sender
            # prints the notification and reports it sent; the
            # on_due hook queues a "Scheduled run due" notice
            # before each due job runs. Job execution below is
            # unchanged: the loop object is not asked to dispatch,
            # it only notifies and drains once per tick.
            from ..capabilities.outbox import Outbox as _ServeOutbox
            from ..interface.scheduler_loop import SchedulerLoop as _ServeLoop
            _serve_outbox = _ServeOutbox(config.home)
            def _serve_sender(item):
                print(f"Outbox notification to {item.get('to', '')}: {item.get('subject', '')}: {item.get('body', '')}")
                return True
            def _serve_on_due(event):
                _serve_outbox.queue("", "Scheduled run due",
                                    f"Scheduled run due: {getattr(event, 'goal', event)}")
            _serve_loop = _ServeLoop(scheduler, dispatcher=None, outbox=_serve_outbox,
                                     outbox_sender=_serve_sender, on_due=_serve_on_due)
            for job in scheduler.check_due(datetime.now()):
                _serve_on_due(job)
                print(f"Due job: {job.name}: {job.goal}")
                from ..agentic.budgets import Budget as _Budget
                from ..agentic.trace import TraceRecorder as _Trace
                from ..agentic.verifier_model import ModelVerifier as _MV
                from ..capabilities.plugins import PluginRegistry as _PR
                _plugins = _PR(config.home); _plugins.load_all()
                try:
                    from ..capabilities.execution import make_backend as _mb
                    _backend = _mb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
                except Exception:
                    _backend = None
                try:
                    from ..agentic.semantic_router import SemanticRouter as _SR2
                    _router2 = _SR2(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
                except Exception:
                    _router2 = None
                from ..interface.run_control import RunControl as _ServeControl
                import uuid as _serve_uuid
                _serve_run_id = _serve_uuid.uuid4().hex[:12]
                _serve_control = _ServeControl(config.home)
                _serve_session = getattr(job, "conversation_id", "") or getattr(job, "project_id", "") or "scheduled"
                _serve_control.register(_serve_run_id, job.goal, _serve_session)
                print(f"Job {job.name}: run id {_serve_run_id}")

                # Scheduled non-goal jobs retain the Aidon scheduler
                # semantics. They do not need a model GoalRunner.
                _handled = False
                if job.kind == "learn":
                    from ..agentic.learn_cycle import run_learn_cycle as _learn
                    from ..capabilities.skill_lifecycle import LifecycleStore
                    _summary = _learn(
                        config.home, sandbox,
                        governor_check=LifecycleStore(config.home).governor_check,
                        max_per_day=max(0, config.learn_max_skills_per_day))
                    print(f"Job {job.name}: learn cycle {_summary}")
                    _handled = True
                elif job.kind == "report":
                    from ..interface.report import daily_report as _daily
                    print(f"Job {job.name}: report\n{_daily(config.home)}")
                    _handled = True
                elif job.kind == "chain":
                    from ..capabilities.chain import (
                        run_chain as _run_chain,
                        run_conditional_chain as _run_cond,
                        validate_chain_spec as _validate_chain,
                        validate_conditional_chain as _validate_cond,
                    )
                    from ..capabilities.tools import ToolRegistry as _ChainTools
                    import json as _chain_json
                    _chain = _chain_json.loads(job.goal)
                    _tools = _ChainTools(
                        sandbox.workdir, sandbox=sandbox,
                        network_available=_network_available(_backend, sandbox))
                    _chain_failures = (
                        _validate_cond(_chain, allowed_tools=tuple(_tools.names()))
                        if isinstance(_chain, list)
                        else _validate_chain(_chain))
                    if _chain_failures:
                        print(f"Job {job.name}: chain rejected: {_chain_failures}")
                        _handled = True
                        continue
                    _chain_decision = _cli_gate(
                        config.home, config.approval_timeout_seconds,
                        _serve_session).ask(
                            "live", f"Approve scheduled chain {job.name}?",
                            json.dumps(_chain, sort_keys=True), force_ask=True)
                    if _chain_decision.verdict not in ("approved", "auto_approved"):
                        print(f"Job {job.name}: chain rejected by approval")
                        _handled = True
                        continue
                    if isinstance(_chain, list):
                        def _invoke(name, args):
                            return _tools.call(name, args)
                        _chain_result = _run_cond(
                            _chain, _invoke,
                            allowed_tools=tuple(_tools.names()))
                    else:
                        _chain_result = _run_chain(_chain, _tools)
                    print(f"Job {job.name}: chain {_chain_result}")
                    _handled = True
                elif job.kind == "brief":
                    from ..interface.report import build_nightly_brief as _brief
                    from ..interface.messaging import MessagingConfig, send_whatsapp
                    _brief_result = _brief(config.home, client)
                    _msg = MessagingConfig(
                        whatsapp_token=config.whatsapp_token,
                        whatsapp_phone_id=config.whatsapp_phone_id,
                        whatsapp_verify_token=config.whatsapp_verify_token,
                        whatsapp_app_secret=config.whatsapp_app_secret,
                        whatsapp_owner=config.whatsapp_owner)
                    _sent = send_whatsapp(_msg, config.whatsapp_owner,
                                          _brief_result["text"])
                    print(f"Job {job.name}: brief delivery {_sent}")
                    _handled = True
                if not _handled:
                    # Scheduled runs get the same extra tools as
                    # interactive runs (fetch, email, web search),
                    # but behind the scheduled gate: email to the
                    # owner goes through, everything else fails
                    # fast instead of blocking the tick on stdin.
                    # Questions have no channel here, so ask_user
                    # tells the model to proceed with its best
                    # judgment and note the assumption. The runner
                    # itself keeps researcher=None on purpose: a
                    # researcher would newly let scheduled goals
                    # route to webchat replies instead of the
                    # agentic pipeline; the web_search plan tool
                    # carries its own researcher.
                    _job_researcher = _make_web_researcher(config)
                    # SSH is an owner PC/VM execution target, not a
                    # sandbox; only container or working bubblewrap
                    # backends can auto-approve scheduled shell steps.
                    from ..capabilities.execution import backend_is_sandboxed
                    _job_sandboxed = backend_is_sandboxed(_backend, sandbox)
                    _job_gate = _scheduled_gate(sandboxed=_job_sandboxed)
                    _job_governor = _make_governor(config)
                    _job_tools = _goal_extra_tools(config, sandbox.workdir, gate=_job_gate, researcher=_job_researcher, question_answer=None, scheduled=True, governor=_job_governor, plugin_registry=_plugins)
                    try:
                        _job_deadline = float(job.time_limit or 0) or None
                    except (TypeError, ValueError):
                        _job_deadline = None
                    runner = GoalRunner(client, sandbox, _job_gate, config.home,
                                        memory=memory, conversation_id=_serve_session, plugins=_plugins,
                                        execution_backend=_backend, budget=_Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                        trace=_Trace(config.home, conversation_id=_serve_session), model_verifier=_MV(client),
                                        kernel_mode=config.kernel_mode, semantic_router=_router2,
                                        use_semantic_router_skip=True, network_available=_network_available(_backend, sandbox), run_id=_serve_run_id, run_control=_serve_control,
                                        extra_tools=_job_tools,
                                        acquisition_governor=_job_governor,
                                        deadline_seconds=_job_deadline)
                    report = runner.run(job.goal)
                    print(f"Job {job.name}: {report.status}, files: {', '.join(report.files)}")
                    # Failure notification for goal jobs, mirroring
                    # the brief kind's WhatsApp send. Failed and
                    # partial (time-box cut) both notify: either
                    # way the job did not complete. Unconfigured
                    # messaging fails soft, like everything else
                    # in this loop — the print above is the floor.
                    if report.status in ("failed", "partial"):
                        try:
                            from ..interface.messaging import MessagingConfig, send_whatsapp
                            _notify_cfg = MessagingConfig(
                                whatsapp_token=config.whatsapp_token,
                                whatsapp_phone_id=config.whatsapp_phone_id,
                                whatsapp_verify_token=config.whatsapp_verify_token,
                                whatsapp_app_secret=config.whatsapp_app_secret,
                                whatsapp_owner=config.whatsapp_owner)
                            _detail = (report.error or "")[:500]
                            _text = (f"Scheduled job {job.name} {report.status}: "
                                     f"{_detail or ', '.join(report.files) or 'no output'}")
                            _sent = send_whatsapp(_notify_cfg, config.whatsapp_owner, _text)
                            print(f"Job {job.name}: failure notification {_sent}")
                        except Exception as exc:
                            print(f"Job {job.name}: failure notification failed: {exc}")
            # Tick drain: once per tick, due notifications only,
            # cadence-spaced by the Outbox itself.
            _serve_loop.drain_outbox()
            # Ingest watcher, same loop, same GoalRunner, no separate daemon.
            try:
                from ..interface.triggers import IngestWatcher
                watcher = IngestWatcher(config.ingest_path, config.home)
                for ingest_event in watcher.events(conversation_id=""):
                    print(f"Ingest trigger: {ingest_event.payload.get('path', '')}")
                    ingest_runner = _full_scheduler_runner()
                    ingest_report = ingest_runner.run(ingest_event.goal)
                    print(f"Ingest job: {ingest_report.status}")
            except Exception as exc:
                print(f"Ingest watch error: {exc}")
            # Slack polling daemon, same loop, channel from AIDON_SLACK_INBOUND_CHANNEL.
            try:
                _channel = _os.environ.get("AIDON_SLACK_INBOUND_CHANNEL", "")
                if config.slack_bot_token and _channel:
                    from ..interface.messaging import MessagingConfig, poll_slack_once
                    _msg_cfg = MessagingConfig(slack_bot_token=config.slack_bot_token)
                    _messages, _slack_last_ts = poll_slack_once(_msg_cfg, _channel, last_ts=_slack_last_ts)
                    _slack_ts_path.write_text(_slack_last_ts)
                    for _message in _messages:
                        print(f"Slack poll message: {_message['text'][:100]}")
                        _poll_runner = _full_scheduler_runner("slack:" + _channel)
                        _poll_report = _poll_runner.run(_message["text"])
                        print(f"Slack poll job: {_poll_report.status}")
            except Exception as exc:
                print(f"Slack poll error: {exc}")
            # Practice windows, same loop: start at most one session
            # per schedule per day while its window is open.
            try:
                from ..interface.practice import (due_for_session, load_schedules,
                                                  save_schedules, window_end)
                from datetime import datetime as _dt
                _now = _dt.now()
                _schedules = load_schedules(config.home)
                _changed = False
                for _sched in _schedules:
                    if due_for_session(_sched, _now):
                        _sched["last_session_date"] = _now.strftime("%Y-%m-%d")
                        _changed = True
                        _end = window_end(_sched, _now)
                        import threading as _th
                        _th.Thread(target=_practice_session_sync, args=(_sched, _end),
                                   daemon=True, name=f"practice-{_sched.get('name')}").start()
                        print(f"Practice session started: {_sched.get('name')}")
                if _changed:
                    save_schedules(config.home, _schedules)
            except Exception as exc:
                print(f"Practice tick error: {exc}")
            import time as _sleep
            _sleep.sleep(60)
    if args.command == "benchmark":
        # Canned Snake to Worm benchmark, for the self-learning claim.
        # Fresh: a long plan JSON, adapted: a short changed-node JSON.
        # Tokens are estimated at one per four characters, as in budgets.
        from ..agentic.benchmark import benchmark_learn
        from ..agentic.counting import CountingClient
        from ..agentic.loop import RunReport, Plan

        class _PlanClient:
            def __init__(self, reply):
                self.reply = reply
                self.calls = 0
            def chat(self, messages, phase="chat"):
                self.calls += 1
                return self.reply
            def chat_phase(self, phase, messages):
                return self.chat(messages, phase=phase)

        long_plan = '{"steps": [{"tool": "write", "args": {"file": "worm.py", "text": "Worm game loop, input handling, rendering, scoring, tests, and a long description of every step in detail."}, "deliverable": "worm.py"}, {"tool": "write", "args": {"file": "worm_test.py", "text": "Tests for the Worm game loop, input, rendering, and scoring, in detail."}, "deliverable": "worm_test.py"}], "deliverables": ["worm.py", "worm_test.py"], "criteria": ["worm.py exists", "worm.py contains Worm"]}'
        short_adapt = '{"adapted": [{"index": 0, "args": {"file": "worm.py", "text": "Worm"}, "deliverable": "worm.py"}]}'
        fresh_client = CountingClient(_PlanClient(long_plan))
        adapted_client = CountingClient(_PlanClient(short_adapt))
        # Fresh and adapted reports, from the counting clients directly,
        # so the benchmark measures model calls, not sandbox execution.
        fresh_client.chat([], phase="plan")
        adapted_client.chat([], phase="adapt")
        fresh_report = RunReport("done", "Write a Worm game", Plan([], ["worm.py"], []), True, True,
                                 files=["worm.py"], model_calls=fresh_client.calls,
                                 model_tokens_estimated=fresh_client.tokens_estimated)
        adapted_report = RunReport("done", "Write a Worm game", Plan([], ["worm.py"], []), True, True,
                                   files=["worm.py"], model_calls=adapted_client.calls,
                                   model_tokens_estimated=adapted_client.tokens_estimated)
        result = benchmark_learn(lambda: (fresh_report, {"worm.py": "Worm game"}),
                                 lambda: (adapted_report, {"worm.py": "Worm game"}),
                                 "Worm", "Snake")
        print(f"Fresh: {result['fresh']['model_calls']} calls, {result['fresh']['model_tokens_estimated']} estimated tokens, quality_ok={result['fresh']['quality_ok']}")
        print(f"Adapted: {result['adapted']['model_calls']} calls, {result['adapted']['model_tokens_estimated']} estimated tokens, quality_ok={result['adapted']['quality_ok']}")
        print(f"Tokens saved, estimated: {result['tokens_saved_estimated']}, saving_counts={result['saving_counts']}")
        print("Canned benchmark, not a live model run. Live benchmark is demo and testing, after work.")
        return 0
    if args.command == "email":
        import os as _os
        if args.action == "drafts":
            drafts_dir = config.home / "email" / "drafts"
            if drafts_dir.exists():
                for draft in sorted(drafts_dir.glob("*.md")):
                    print(draft.name)
            return 0
        # send: Gmail OAuth, only if the keys are present, else drafts only.
        client_id = _os.environ.get("GMAIL_CLIENT_ID", "")
        client_secret = _os.environ.get("GMAIL_CLIENT_SECRET", "")
        refresh_token = _os.environ.get("GMAIL_REFRESH_TOKEN", "")
        if not (client_id and client_secret and refresh_token):
            print("Gmail OAuth keys are missing, email stays as drafts. Fill GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN in .env, then run again.")
            return 2
        from ..capabilities.gmail import gmail_sender
        sender = gmail_sender(client_id, client_secret, refresh_token)
        result = sender(args.to or "", args.subject, args.body)
        print(f"Gmail send result: {result.get('id', 'sent')}")
        return 0
    from ..capabilities.execution import make_backend
    try:
        execution_backend = make_backend(
            config.execution_backend, sandbox,
            image=config.docker_image, allow_network=config.docker_network,
            vm_host=config.vm_host, vm_user=config.vm_user or None,
            vm_sudo_password=config.vm_sudo_password or None,
            vm_use_sudo=config.vm_use_sudo,
            modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                    modal_memory_mb=config.modal_memory_mb)
    except Exception as exc:
        print(f"execution backend error: {exc}")
        return 2
    if args.command == "plugin":
        from pathlib import Path
        from ..capabilities.plugins import PluginRegistry, scan_plugin
        from ..capabilities.plugin_acceptance import acceptance_backend
        registry = PluginRegistry(config.home)
        if args.action == "list":
            plugins = registry.load_all()
            for plugin in plugins:
                print(f"{plugin.name} {plugin.version}: {', '.join(plugin.tool_names()) or 'no tools'}")
            return 0
        if args.path is None:
            print("plugin scan and adopt need a directory path")
            return 2
        if args.action == "scan":
            findings = scan_plugin(Path(args.path))
            for finding in findings:
                print(f"{finding.path}:{finding.line} {finding.type}")
            print(f"findings: {len(findings)}")
            return 0
        ok, findings, message = registry.adopt(
            Path(args.path), gate=_cli_gate(config.home, config.approval_timeout_seconds), memory=memory,
            test_backend_factory=lambda: acceptance_backend(config, sandbox.workdir),
            conversation_id=args.conversation,
            tool_posture=str(getattr(config, "tool_posture", "standard")
                             or "standard").strip().lower(),
            kernel_mode=bool(getattr(config, "kernel_mode", False)))
        print(message)
        for finding in findings:
            print(f"{finding.path}:{finding.line} {finding.type}")
        return 0 if ok else 2
    if args.command == "interview":
        # Semantic routing, interview surface too: a chat goal does not
        # start an interview or a spec, it gets a direct chat answer.
        try:
            from ..agentic.semantic_router import SemanticRouter
            _router = SemanticRouter(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
            _route, _conf = _router.classify(args.text)
            if _route == "chat" and _conf >= 0.15 and not any(w in args.text.lower() for w in ("write", "save", ".md", ".py")):
                from .model_client import Message as _Msg
                print(client.chat([_Msg("user", args.text)]))
                return 0
        except Exception:
            pass
        from ..agentic.interview import InterviewRunner
        runner = InterviewRunner(client, config.home, memory=memory,
                                 gate=_cli_gate(config.home, config.approval_timeout_seconds), conversation_id=args.conversation)
        print(f"Spec template: {runner.ensure_template()}  "
              f"(copy it, fill it in, attach it to skip the interview; "
              f"docs: README, Spec sheet section)")
        if args.attach:
            # Attached spec sheet: skip the interview questions.
            from pathlib import Path
            result = runner.load_attached_spec(
                Path(args.attach).read_text(encoding="utf-8"))
        else:
            # Questions are chat, no clock, no card: input() in CLI.
            result = runner.start_from_message(
                args.text, ask=lambda q: input(f"\nAidon: {q}\nYou: "))
        print(f"\nSpec verified: {result.verified}")
        if result.failures:
            print(f"Spec failures: {'; '.join(result.failures)}")
        print(f"Spec approved: {result.approved}")
        print(f"Spec file: {config.home}/specs/{args.conversation}.json")
        print(f"Spec sheet: {config.home}/specs/{args.conversation}.md")
        if not result.approved:
            return 2
        if not args.run:
            return 0
        from ..capabilities.plugins import PluginRegistry
        plugins = PluginRegistry(config.home)
        plugins.load_all()
        def _interview_researcher(goal_text):
            try:
                from ..agentic.research_allow import DomainGrants, load_allowlist
                from ..capabilities.search import search as _search
                result = _search(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                                 allowlist=load_allowlist(config.home, config.research_allowlist),
                                 grants=DomainGrants(config.home))
                return [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in result.get("results", [])]
            except Exception:
                return []
        # Full runner, same as the CLI goal run: budget, trace, model
        # verification, semantic routing, and run control. The interview
        # branch previously built a thin runner with none of these, so
        # caps, verification, and steer/cancel silently did not apply.
        from ..agentic.budgets import Budget as _IBudget
        from ..agentic.trace import TraceRecorder as _ITrace
        from ..agentic.verifier_model import ModelVerifier as _IModelVerifier
        from ..interface.run_control import RunControl as _IRunControl
        try:
            from ..agentic.semantic_router import SemanticRouter as _ISemanticRouter
            _i_router = _ISemanticRouter(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
        except Exception:
            _i_router = None
        import uuid as _i_uuid
        _i_run_id = _i_uuid.uuid4().hex[:12]
        _i_control = _IRunControl(config.home)
        _i_control.register(_i_run_id, args.text, args.conversation)
        print(f"interview run id: {_i_run_id}")
        _i_governor = _make_governor(config)
        goal_runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home,
                                 memory=memory,
                                 conversation_id=args.conversation, plugins=plugins, execution_backend=execution_backend, researcher=_interview_researcher, extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=_cli_gate(config.home, config.approval_timeout_seconds), researcher=_interview_researcher, governor=_i_governor, plugin_registry=plugins), router=model_router, versioned_writes=True,
                                 budget=_IBudget(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                 trace=_ITrace(config.home, conversation_id=args.conversation),
                                 model_verifier=_IModelVerifier(client),
                                 semantic_router=_i_router,
                                 use_semantic_router_skip=True,
                                 run_control=_i_control, run_id=_i_run_id, network_available=_network_available(execution_backend, sandbox), acquisition_governor=_i_governor)
        report = goal_runner.run(args.text, spec=result.spec)
        print(f"status: {report.status}")
        print(f"verified: {report.verified}")
        print(f"approved: {report.approved}")
        print(f"files: {', '.join(report.files)}")
        print(f"attempts: {report.attempts}")
        print(format_run_stats(report))
        if report.error:
            print(f"error: {report.error}")
            return 1
        return 0 if report.status == "done" else 2
    from ..capabilities.plugins import PluginRegistry
    plugins = PluginRegistry(config.home)
    plugins.load_all()
    from ..agentic.budgets import Budget
    from ..agentic.trace import TraceRecorder
    budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                    max_seconds=config.budget_seconds)
    trace = TraceRecorder(config.home, conversation_id=args.conversation)
    from ..agentic.verifier_model import ModelVerifier
    model_verifier = ModelVerifier(client)
    try:
        from ..agentic.semantic_router import SemanticRouter as _SR
        _cli_router = _SR(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
    except Exception:
        _cli_router = None
    def _researcher(goal_text):
        try:
            from ..agentic.research_allow import DomainGrants, load_allowlist
            from ..capabilities.search import search as _search
            result = _search(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                             allowlist=load_allowlist(config.home, config.research_allowlist),
                             grants=DomainGrants(config.home))
            return [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in result.get("results", [])]
        except Exception:
            return []
    import uuid as _uuid
    _goal_run_id = _uuid.uuid4().hex[:12]
    from ..interface.run_control import RunControl as _GoalControl
    _goal_control = _GoalControl(config.home)
    _goal_control.register(_goal_run_id, args.text, args.conversation)
    print(f"run id: {_goal_run_id}")
    _governor = _make_governor(config)
    runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds, args.conversation), config.home,
                        memory=memory,
                        conversation_id=args.conversation, plugins=plugins, execution_backend=execution_backend,
                        budget=budget, trace=trace, model_verifier=model_verifier,
                        kernel_mode=config.kernel_mode, semantic_router=_cli_router,
                        use_semantic_router_skip=True, researcher=_researcher,
                        extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=_cli_gate(config.home, config.approval_timeout_seconds, args.conversation), researcher=_researcher, model_chat=lambda messages: client.chat(messages), governor=_governor, plugin_registry=plugins),
                        router=model_router, versioned_writes=True,
                        run_id=_goal_run_id, run_control=_goal_control,
                        network_available=_network_available(execution_backend, sandbox),
                        guidance=getattr(args, "guidance", ""), acquisition_governor=_governor)
    spec = None
    if getattr(args, "spec", None):
        from pathlib import Path
        from ..agentic.interview import InterviewRunner, parse_spec_sheet
        spec = parse_spec_sheet(Path(args.spec).read_text(encoding="utf-8"))
        verified, failures = spec.verify()
        if not verified:
            print(f"Attached spec is not verified: {'; '.join(failures)}")
            return 2
    if getattr(args, "swarm", False):
        try:
            from ..agentic.swarm import run_swarm
            def _swarm_runner(subgoal):
                # Per-subgoal runner, not the shared main runner: each
                # sub-run gets its own run_id (never the parent's), and
                # the research + routing the main runner uses. Sub-runs
                # register under their own ids, so control list shows
                # them and cancel/steer can target a sub-run directly.
                _sub_id = f"{runner.run_id}-{_uuid.uuid4().hex[:8]}"
                _goal_control.register(_sub_id, subgoal, args.conversation)
                sub_runner = GoalRunner(client, sandbox, runner.gate,
                                        config.home, memory=memory,
                                        conversation_id=args.conversation, plugins=plugins,
                                        execution_backend=execution_backend,
                                        budget=budget, trace=trace,
                                        model_verifier=model_verifier,
                                        kernel_mode=config.kernel_mode,
                                        semantic_router=_cli_router,
                                        use_semantic_router_skip=True,
                                        researcher=_researcher,
                                        extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=runner.gate, researcher=_researcher, governor=getattr(runner, "acquisition_governor", None), plugin_registry=plugins),
                                        router=model_router, versioned_writes=True,
                                        run_id=_sub_id, run_control=_goal_control, network_available=_network_available(execution_backend, sandbox), acquisition_governor=getattr(runner, "acquisition_governor", None))
                sub_report = sub_runner.run(subgoal, spec=spec)
                return f"[{sub_report.status}] {', '.join(sub_report.files)} {sub_report.error}".strip()
            swarm_result = run_swarm(args.text, client, _swarm_runner)
            if swarm_result.get("swarmed"):
                print(f"Swarm, subgoals: {', '.join(swarm_result['subgoals'])}")
                print(swarm_result["merged"])
                print("status: swarm")
                return 0
        except Exception as exc:
            print(f"Swarm failed, falling back to single run: {exc}")
    report = runner.run(args.text, spec=spec)
    print(f"status: {report.status}")
    print(f"verified: {report.verified}")
    print(f"approved: {report.approved}")
    print(f"files: {', '.join(report.files)}")
    print(f"attempts: {report.attempts}")
    print(format_run_stats(report))
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
