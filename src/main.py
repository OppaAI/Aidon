"""Command line for Aidon.

Usage:
  python -m src.main goal "Plan a dinner for two"
  python -m src.main goal --project dinner "Plan a dinner for two"

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
from previous projects. If the real embedder is not
installed, the CLI falls back to HashEmbedder with a
warning on stderr, so a run still works offline, with
non-semantic recall. The demo and submission run on the
real embedder, not on the fallback.
"""

from __future__ import annotations

import argparse
import sys

from .approval import ApprovalGate, Decision
from .config import Config
from .loop import GoalRunner
from .model_client import FakeClient, HttpModelClient
from .sandbox import Sandbox


def _cli_gate() -> ApprovalGate:
    def decide(kind: str, title: str, body: str) -> Decision:
        print(f"\n[{kind}] {title}\n{body}\n")
        answer = input("Approve? [y/N] ").strip().lower()
        return Decision("approved" if answer in ("y", "yes") else "rejected")

    return ApprovalGate(decide)


def _promotion_ask(point) -> bool:
    answer = input(
        f"\nPromote this run's episodic memory to long-term memory? "
        f"[y/N] ").strip().lower()
    return answer in ("y", "yes")


def _make_memory(config: Config):
    """Memory with the real embedder if available, else Hash, with warning."""
    try:
        from .embedder import HashEmbedder, make_embedder_cached
        from .memory import Memory
        embedder = make_embedder_cached()
        return Memory(config.home, embedder=embedder)
    except Exception as exc:
        print(f"warning: real embedder unavailable ({exc}), "
              "using HashEmbedder for this run", file=sys.stderr)
        from .embedder import HashEmbedder
        from .memory import Memory
        return Memory(config.home, embedder=HashEmbedder())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidon")
    sub = parser.add_subparsers(dest="command", required=True)
    goal_parser = sub.add_parser("goal", help="Run a goal")
    goal_parser.add_argument("text", help="Goal in plain language")
    goal_parser.add_argument("--project", default="default",
                             help="Project id for episodic memory")
    goal_parser.add_argument("--spec", default=None,
                             help="Attached spec sheet file, skips interview")
    interview_parser = sub.add_parser("interview", help="Interview to a spec")
    interview_parser.add_argument("text", help="Goal in plain language, chat message")
    interview_parser.add_argument("--project", default="default",
                                  help="Project id for episodic memory")
    interview_parser.add_argument("--run", action="store_true",
                                  help="Run the goal after the spec is approved")
    interview_parser.add_argument("--attach", default=None,
                                  help="Attached spec sheet file, skips interview questions")
    plugin_parser = sub.add_parser("plugin", help="Plugin tools and skills")
    plugin_parser.add_argument("action", choices=["list", "scan", "adopt"],
                               help="List adopted plugins, scan a plugin dir, or adopt it")
    plugin_parser.add_argument("path", nargs="?", default=None,
                               help="Plugin directory, for scan and adopt")
    plugin_parser.add_argument("--project", default="default",
                               help="Project id for episodic memory")
    trigger_parser = sub.add_parser("trigger", help="Trigger a DAG run")
    trigger_parser.add_argument("action", choices=["list", "ingest", "schedule", "webhook"],
                                help="List sources, scan ingest folder, check due jobs, or fire a webhook trigger")
    trigger_parser.add_argument("name", nargs="?", default=None,
                                help="Webhook trigger name, for webhook action")
    trigger_parser.add_argument("--goal", default="",
                                help="Goal for a webhook trigger")
    trigger_parser.add_argument("--project", default="default",
                                help="Project id for episodic memory")
    v1_parser = sub.add_parser("v1", help="v1 API, token auth, MCP")
    v1_parser.add_argument("action", choices=["serve", "keygen", "mcp"],
                           help="Serve the v1 HTTP API, generate a key once, or run the MCP stdio server")
    v1_parser.add_argument("--port", type=int, default=8001,
                           help="Port for v1 serve, default 8001, localhost only")
    v1_parser.add_argument("--name", default="owner",
                           help="Key name for keygen")
    v1_parser.add_argument("--scope", default="owner", choices=["owner", "agent"],
                           help="Key scope for keygen")
    webui_parser = sub.add_parser("webui", help="WebUI server and messaging webhooks")
    webui_parser.add_argument("action", choices=["serve"], help="Serve the WebUI, localhost only")
    webui_parser.add_argument("--port", type=int, default=8002,
                              help="Port for WebUI serve, default 8002, localhost only")
    args = parser.parse_args(argv)

    config = Config.load()
    config.ensure_home()
    if config.api_base:
        client = HttpModelClient(config.api_base, config.api_key,
                                 config.model)
    else:
        client = FakeClient("")
    sandbox = Sandbox(config.workdir, config.sandbox_timeout_seconds)
    memory = _make_memory(config)
    if args.command == "webui":
        from .messaging import MessagingConfig, send_slack, send_whatsapp
        from .triggers import TriggerDispatcher
        from .v1 import AuthStore
        from .webui import WebUIServer
        msg = MessagingConfig.from_env()
        # Override from Config, which already read the old names.
        msg.whatsapp_token = config.whatsapp_token or msg.whatsapp_token
        msg.whatsapp_phone_id = config.whatsapp_phone_id or msg.whatsapp_phone_id
        msg.whatsapp_verify_token = config.whatsapp_verify_token or msg.whatsapp_verify_token
        msg.whatsapp_app_secret = config.whatsapp_app_secret or msg.whatsapp_app_secret
        msg.whatsapp_owner = config.whatsapp_owner or msg.whatsapp_owner
        msg.slack_bot_token = config.slack_bot_token or msg.slack_bot_token
        msg.slack_signing_secret = config.slack_signing_secret or msg.slack_signing_secret
        dispatcher = TriggerDispatcher()
        # WebUI chat handler, for the server core: echo the
        # goal back, a real goal handler is registered by
        # the caller with GoalRunner, interview, or triggers.
        dispatcher.register("webui", lambda e: e.send_reply(f"Received: {e.goal}"))
        dispatcher.register("whatsapp", lambda e: e.send_reply(f"Received: {e.goal}"))
        dispatcher.register("slack", lambda e: e.send_reply(f"Received: {e.goal}"))

        def whatsapp_sender(to: str, text: str):
            result = send_whatsapp(msg, to, text)
            if not result["ok"]:
                print(f"WhatsApp send failed: {result['error']}")

        def slack_sender(channel: str, text: str, thread_ts=None):
            result = send_slack(msg, channel, text, thread_ts=thread_ts)
            if not result["ok"]:
                print(f"Slack send failed: {result['error']}")

        server = WebUIServer(config.home, dispatcher=dispatcher,
                             auth_store=AuthStore(config.home), memory=memory,
                             messaging_config=msg,
                             whatsapp_sender=whatsapp_sender,
                             slack_sender=slack_sender)
        missing = msg.whatsapp_missing()
        if missing:
            print(f"WhatsApp not fully configured, missing: {', '.join(missing)}")
        if not msg.whatsapp_owner:
            print("WhatsApp inbound disabled: AIDON_WHATSAPP_OWNER not set, owner only, fail closed.")
        if not msg.whatsapp_app_secret:
            print("Warning: AIDON_WHATSAPP_APP_SECRET not set, webhook signature check is off.")
        if not msg.slack_bot_token:
            print("Slack send not configured: SLACK_BOT_TOKEN not set.")
        if not msg.slack_signing_secret:
            print("Warning: SLACK_SIGNING_SECRET not set, Slack signature check is off.")
        print(f"WebUI on http://127.0.0.1:{args.port}/, localhost only, WhatsApp and Slack webhooks at /webhook/whatsapp and /webhook/slack")
        print("Public reachability still needs a tunnel, localhost alone is not reachable by Meta or Slack.")
        server.serve(port=args.port)
        return 0
    if args.command == "v1":
        from .v1 import AuthStore, GoalStore, handle
        auth = AuthStore(config.home)
        goals = GoalStore(config.home)
        if args.action == "keygen":
            key, record = auth.create_key(args.name, args.scope)
            print("Save this key now, it is shown once, only its hash is stored:")
            print(key)
            print(f"Name: {record.name}, scope: {record.scope}, hash prefix: {record.key_hash[:12]}")
            return 0
        if args.action == "mcp":
            from .mcp_server import MCPServer
            MCPServer(auth, goals).run_stdio()
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
                                         dict(self.headers), body)
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
        from .triggers import (ALLOWED_SOURCES, IngestWatcher, Scheduler,
                               TriggerDispatcher, handle_webhook)
        if args.action == "list":
            print("Trigger sources, all start a DAG run, none bypass approval:")
            for source in ALLOWED_SOURCES:
                print(f"  {source}")
            print("  No Telegram source, by owner decision.")
            return 0
        if args.action == "ingest":
            watcher = IngestWatcher(config.ingest_path, config.home)
            for event in watcher.events(project_id=args.project):
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
                                   project_id=args.project)
            dispatcher = TriggerDispatcher()
            dispatcher.register("webhook", lambda e: print(f"webhook trigger: {e.kind}: {e.goal}"))
            dispatcher.dispatch(event)
            return 0
        return 2
    from .execution import make_backend
    try:
        execution_backend = make_backend(
            config.execution_backend, sandbox,
            image=config.docker_image, allow_network=config.docker_network)
    except Exception as exc:
        print(f"execution backend error: {exc}")
        return 2
    if args.command == "plugin":
        from pathlib import Path
        from .plugins import PluginRegistry, scan_plugin
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
            Path(args.path), gate=_cli_gate(), memory=memory,
            project_id=args.project)
        print(message)
        for finding in findings:
            print(f"{finding.path}:{finding.line} {finding.type}")
        return 0 if ok else 2
    if args.command == "interview":
        from .interview import InterviewRunner
        runner = InterviewRunner(client, config.home, memory=memory,
                                 gate=_cli_gate(), project_id=args.project)
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
        print(f"Spec file: {config.home}/specs/{args.project}.json")
        print(f"Spec sheet: {config.home}/specs/{args.project}.md")
        if not result.approved:
            return 2
        if not args.run:
            return 0
        from .plugins import PluginRegistry
        plugins = PluginRegistry(config.home)
        plugins.load_all()
        goal_runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                                 memory=memory, promotion_ask=_promotion_ask,
                                 project_id=args.project, plugins=plugins, execution_backend=execution_backend)
        report = goal_runner.run(args.text, spec=result.spec)
        print(f"status: {report.status}")
        print(f"verified: {report.verified}")
        print(f"approved: {report.approved}")
        print(f"files: {', '.join(report.files)}")
        print(f"attempts: {report.attempts}")
        if report.error:
            print(f"error: {report.error}")
            return 1
        return 0 if report.status == "done" else 2
    from .plugins import PluginRegistry
    plugins = PluginRegistry(config.home)
    plugins.load_all()
    from .budgets import Budget
    from .trace import TraceRecorder
    budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                    max_seconds=config.budget_seconds)
    trace = TraceRecorder(config.home, project_id=args.project)
    runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                        memory=memory, promotion_ask=_promotion_ask,
                        project_id=args.project, plugins=plugins, execution_backend=execution_backend,
                        budget=budget, trace=trace)
    spec = None
    if getattr(args, "spec", None):
        from pathlib import Path
        from .interview import InterviewRunner, parse_spec_sheet
        spec = parse_spec_sheet(Path(args.spec).read_text(encoding="utf-8"))
        verified, failures = spec.verify()
        if not verified:
            print(f"Attached spec is not verified: {'; '.join(failures)}")
            return 2
    report = runner.run(args.text, spec=spec)
    print(f"status: {report.status}")
    print(f"verified: {report.verified}")
    print(f"approved: {report.approved}")
    print(f"files: {', '.join(report.files)}")
    print(f"attempts: {report.attempts}")
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
