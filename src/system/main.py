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
import json
import sys

from ..interface.approval import ApprovalGate, Decision
from .config import Config
from ..agentic.loop import GoalRunner
from .model_client import FakeClient
from ..capabilities.sandbox import Sandbox


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
        from ..memory.embedder import make_embedder_cached
        from ..memory.entity_graph import EntityGraph
        from ..memory.memory import Memory
        embedder = make_embedder_cached()
        return Memory(config.home, embedder=embedder, entity_graph=EntityGraph(config.home))
    except Exception as exc:
        print(f"warning: real embedder unavailable ({exc}), "
              "using HashEmbedder for this run", file=sys.stderr)
    try:
        from ..memory.embedder import HashEmbedder
        from ..memory.memory import Memory
        return Memory(config.home, embedder=HashEmbedder())
    except Exception as exc:
        # A broken Qdrant install must not make every command fail.
        # The in-process store keeps the run working for this process,
        # and says so, rather than pretending it persisted anything.
        print(f"warning: Qdrant unavailable ({exc}), using in-process "
              "memory for this run, nothing persists", file=sys.stderr)
        from ..memory.embedder import HashEmbedder
        from ..memory.memory import InMemoryStore, Memory
        return Memory(config.home, embedder=HashEmbedder(),
                      store=InMemoryStore())


class _LazyMemory:
    """Build memory on first use, not at CLI start.

    Opening Qdrant and loading the 440M embedder costs seconds and
    hundreds of megabytes. Commands such as ``tools list``,
    ``schedule list``, and ``sandbox check`` never touch memory, so
    paying that cost before dispatch is pure startup latency. This
    proxy defers construction until a memory attribute is actually
    read.
    """

    def __init__(self, config: Config):
        self._config = config
        self._memory = None

    def _get(self):
        if self._memory is None:
            self._memory = _make_memory(self._config)
        return self._memory

    def __getattr__(self, name):
        return getattr(self._get(), name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidon")
    sub = parser.add_subparsers(dest="command", required=True)
    goal_parser = sub.add_parser("goal", help="Run a goal")
    goal_parser.add_argument("text", help="Goal in plain language")
    goal_parser.add_argument("--project", default="default",
                             help="Project id for episodic memory")
    goal_parser.add_argument("--swarm", action="store_true", help="Attempt swarm split, Aidon decides, fallback to single run")
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
    v1_parser.add_argument("action", choices=["serve", "keygen", "mcp", "mcp-http", "revoke"],
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
    webui_parser.add_argument("--webhook-only", action="store_true",
                              help="Serve only /webhook/whatsapp and /webhook/slack, no chat, settings, or library, for a tunnel front")
    scheduler_parser = sub.add_parser("scheduler", help="Scheduler daemon and pre-approval")
    scheduler_parser.add_argument("action", choices=["serve", "preapprove", "morning"],
                                  help="Serve due jobs, grant a pre-approval, or list the morning report")
    scheduler_parser.add_argument("--spec", default="default", help="Spec id for pre-approval")
    scheduler_parser.add_argument("--kind", default="sandbox", help="Action kind for pre-approval, reversible only")
    scheduler_parser.add_argument("--hours", type=float, default=8.0, help="Pre-approval duration, hours")
    benchmark_parser = sub.add_parser("benchmark", help="Self-learning benchmark, canned, not live")
    benchmark_parser.add_argument("action", choices=["learn"], help="Snake to Worm, fresh vs adapted")
    control_parser = sub.add_parser("control", help="Steer and cancel a running run")
    control_parser.add_argument("action", choices=["list", "cancel", "steer"], help="List runs, cancel a run, or steer a run")
    control_parser.add_argument("run_id", nargs="?", default=None, help="Run id, from the goal reply")
    control_parser.add_argument("note", nargs="?", default=None, help="Steer note, for steer")
    sam_parser = sub.add_parser("sam", help="SAM 3.1 segmentation for video editing, segment and track objects in images or frames")
    sam_parser.add_argument("prompt", help="Noun phrase, for example red toolbox")
    sam_parser.add_argument("images", nargs="+", help="Image or video frame file paths")
    sam_parser.add_argument("--propagation", default="both", choices=["both", "forward", "backward"])
    video_parser = sub.add_parser("video", help="Video pipeline: extract frames, SAM segment, annotate, re-encode with ffmpeg, so Aidon can edit his own demo video")
    video_parser.add_argument("action", choices=["edit", "extract", "encode"], help="edit = full pipeline, extract = frames only, encode = frames to video")
    video_parser.add_argument("input", help="Input video for edit and extract, or frames directory for encode")
    video_parser.add_argument("output", help="Output video for edit and encode, or frames directory for extract")
    video_parser.add_argument("--prompt", default="", help="Noun phrase for SAM segmentation, for edit, for example red toolbox")
    video_parser.add_argument("--fps", type=float, default=1.0, help="Frames per second, default 1")
    image_parser = sub.add_parser("image", help="Muse Image generation and editing, same .env key and endpoint as Spark")
    image_parser.add_argument("action", choices=["generate", "edit"], help="generate = prompt only, edit = prompt plus 1-10 reference images")
    image_parser.add_argument("prompt", help="Prompt, for edit refer to refs as @Image1, @Image2 in input order")
    image_parser.add_argument("--refs", nargs="*", default=[], help="Reference image URLs or file paths, for edit, 1 to 10")
    image_parser.add_argument("--size", default="1024x1024")
    image_parser.add_argument("--output", default="")
    unity_parser = sub.add_parser("unity", help="Unity 3D assets, generated by Aidon")
    unity_parser.add_argument("action", choices=["asset", "build"], help="Generate a Unity-importable asset, or print the Unity batch build command")
    unity_parser.add_argument("name", help="Asset name, for example Worm")
    unity_parser.add_argument("--shape", default="cube", choices=["cube", "sphere", "cylinder"], help="Primitive shape")
    unity_parser.add_argument("--color", default="blue", help="Color name, for the C# material")
    email_parser = sub.add_parser("email", help="Email drafts and Gmail send")
    email_parser.add_argument("action", choices=["drafts", "send"], help="List drafts, or send a draft via Gmail OAuth")
    email_parser.add_argument("--draft", default=None, help="Draft id for send")
    email_parser.add_argument("--to", default=None, help="Recipient for send")
    email_parser.add_argument("--subject", default="", help="Subject for send")
    email_parser.add_argument("--body", default="", help="Body for send")

    memory_parser = sub.add_parser("memory", help="Inspect and prune Aidon memory, old Aidon memory commands")
    memory_parser.add_argument("action", choices=["forget", "ingest", "check", "prune", "list"], help="forget = delete matching memories, ingest = ingest a folder, check = list points, prune = delete decayed memories, list = list projects")
    memory_parser.add_argument("query", nargs="?", default="", help="Query for forget, folder for ingest")
    memory_parser.add_argument("--id", default=None, help="Exact point id for forget")
    memory_parser.add_argument("--collection", default="episodic", help="Collection for forget, check, prune")
    memory_parser.add_argument("--project", default="default", help="Project id")
    memory_parser.add_argument("--dry-run", action="store_true", help="Prune dry run only, do not delete")
    skills_parser = sub.add_parser("skills", help="Inspect the skill library, old Aidon skills commands")
    skills_parser.add_argument("action", choices=["list", "show", "check", "rollback"], help="list = list skills, show = show one skill state, check = governor check, rollback = reset a skill to candidate")
    skills_parser.add_argument("name", nargs="?", default="", help="Skill name")
    tools_parser = sub.add_parser("tools", help="Inspect the tool registry")
    tools_parser.add_argument("action", choices=["list", "check"], help="list = list plugin tools, check = check a tool exists")
    tools_parser.add_argument("name", nargs="?", default="", help="Tool name for check")
    dag_parser = sub.add_parser("dag", help="Inspect saved DAG workflows")
    dag_parser.add_argument("action", choices=["list", "show"], help="list = list saved DAGs, show = show one DAG")
    dag_parser.add_argument("id", nargs="?", default="", help="DAG id for show")
    mission_parser = sub.add_parser("mission", help="Mission lifecycle records")
    mission_parser.add_argument("action", choices=["list", "show", "discard", "create"], help="list = list missions, show = show one, discard = discard a rejected mission, create = create a draft mission")
    mission_parser.add_argument("id", nargs="?", default="", help="Project id")
    mission_parser.add_argument("--spec", default="{}", help="Spec JSON for create")
    outbox_parser = sub.add_parser("outbox", help="Durable notification outbox")
    outbox_parser.add_argument("action", choices=["list", "drain", "queue"], help="list = list queued, drain = deliver queued with a printed sender, queue = queue a notification")
    outbox_parser.add_argument("--to", default="", help="Recipient for queue")
    outbox_parser.add_argument("--subject", default="", help="Subject for queue")
    outbox_parser.add_argument("--body", default="", help="Body for queue")
    export_parser = sub.add_parser("export", help="Render a read-only markdown view of the registry, and export memory points")
    export_parser.add_argument("--dir", default="", help="Output directory, default <home>/export")
    export_parser.add_argument("--registry", action="store_true", help="Export the full registry to Markdown, in addition to memory points")
    schedule_parser = sub.add_parser("schedule", help="Cron-like scheduled jobs, old Aidon schedule commands, scheduler serve runs them")
    schedule_parser.add_argument("action", choices=["add", "list", "remove", "show"], help="add = add a job, list = list jobs, remove = remove a job, show = show a job")
    schedule_parser.add_argument("--name", default="", help="Job name")
    schedule_parser.add_argument("--goal", default="", help="Goal for add")
    schedule_parser.add_argument("--schedule", default="0 9 * * *", help="Cron schedule for add, default daily 9am")
    schedule_parser.add_argument("--project", default="default", help="Project id for add")
    sandbox_parser = sub.add_parser("sandbox", help="Sandbox isolation boundary")
    sandbox_parser.add_argument("action", choices=["check"], help="check = report sandbox backend and workdir, fail closed if backend missing")
    chain_parser = sub.add_parser("chain", help="Declarative tool chains, zero-LLM scheduled jobs")
    chain_parser.add_argument("action", choices=["validate", "run"], help="validate = validate a chain spec JSON, run = run a validated chain through sandbox tools")
    chain_parser.add_argument("path", help="Chain spec JSON file")
    chat_parser = sub.add_parser("chat", help="Interactive terminal chat, old Aidon chat command, type a goal, empty line to exit")
    computer_parser = sub.add_parser("computer", help="Screenshot and computer use, approval-gated, no GUI action without a driver")
    computer_parser.add_argument("action", choices=["screenshot", "run"], help="screenshot = capture screen to a file, run = run a computer-use action JSON file")
    computer_parser.add_argument("path", help="Output PNG for screenshot, or action JSON file for run")
    computer_parser.add_argument("--approve", action="store_true", help="Approve irreversible actions in this run, otherwise they are refused")

    report_parser = sub.add_parser("report", help="Autonomy and daily reports, old Aidon report commands")
    report_parser.add_argument("action", choices=["autonomy", "daily"], help="autonomy = counts and gate status, daily = daily report text")
    demo_parser = sub.add_parser("demo", help="Demo learning and invent-tool, live, not canned")
    demo_parser.add_argument("action", choices=["learn", "invent-tool"], help="learn = real experience counts, invent-tool = draft a plugin skeleton and scan it, adoption still needs the gate")
    demo_parser.add_argument("--name", default="", help="Plugin name for invent-tool")
    demo_parser.add_argument("--description", default="", help="Plugin description for invent-tool")
    slack_parser = sub.add_parser("slack", help="Slack inbound listening, old Aidon slack listen")
    slack_parser.add_argument("action", choices=["listen", "status"], help="listen = poll Slack once or for --seconds, status = configured or not")
    slack_parser.add_argument("--channel", default="", help="Slack channel id, or AIDON_SLACK_INBOUND_CHANNEL")
    slack_parser.add_argument("--seconds", type=int, default=0, help="Listen duration in seconds, 0 = one poll only")
    slack_parser.add_argument("--run", action="store_true", help="Run polled messages as goals, otherwise just print them")
    whatsapp_parser = sub.add_parser("whatsapp", help="WhatsApp status and send, old Aidon whatsapp commands")
    whatsapp_parser.add_argument("action", choices=["status", "send"], help="status = configured or missing, send = send a real WhatsApp message, fails if not configured, never faked")
    whatsapp_parser.add_argument("--to", default="", help="Recipient number for send")
    whatsapp_parser.add_argument("--text", default="", help="Message text for send")
    tick_parser = sub.add_parser("tick", help="Run due scheduler jobs once, old Aidon tick")
    repl_parser = sub.add_parser("repl", help="Full REPL, old Aidon repl, /help, /history, /quit, or a goal")
    args = parser.parse_args(argv)

    config = Config.load()
    config.ensure_home()
    from .model_client import client_for_config
    try:
        client = client_for_config(config)
    except ValueError as exc:
        print(f"model backend error: {exc}", file=sys.stderr)
        return 2
    if isinstance(client, FakeClient):
        print("warning: no model backend configured, using FakeClient; "
              "set AIDON_BACKEND and AIDON_API_ENDPOINT (or "
              "AIDON_META_API_KEY) in .env for a real run", file=sys.stderr)
    # Fast model, same backend, different model name. This is how a
    # local small model works too: set the endpoint to a local
    # Ollama, AIDON_MODEL to the strong model, and AIDON_FAST_MODEL
    # to the small local model. No Ollama-specific branch in the loop.
    if config.fast_model and config.fast_model != config.model:
        from dataclasses import replace
        from ..agentic.router import RoutedClient
        fast_config = replace(
            config, model=config.fast_model,
            ollama_num_ctx=config.fast_num_ctx or config.ollama_num_ctx)
        fast_client = client_for_config(fast_config)
        fast_phases = {s.strip() for s in config.router_fast_phases.split(",") if s.strip()}
        client = RoutedClient(fast_client, client, fast_phases=fast_phases)
    sandbox = Sandbox(config.workdir, config.sandbox_timeout_seconds)
    memory = _LazyMemory(config)

    if args.command == "memory":
        from ..memory.kb import KnowledgeBase
        if args.action == "forget":
            if args.id:
                ok = memory.forget(args.collection, args.id)
                print(f"Forgot {args.id}: {ok}")
                return 0 if ok else 2
            result = memory.forget_search(args.query) if args.query else {"matches": []}
            print(f"Forget matches for {args.query!r}: {result}")
            return 0
        if args.action == "ingest":
            from pathlib import Path as _P
            kb = KnowledgeBase(memory)
            folder = _P(args.query)
            total = 0
            for file_path in sorted(folder.rglob("*")) if folder.is_dir() else [folder]:
                if file_path.is_file():
                    try:
                        total += kb.ingest_file(file_path)
                    except Exception as exc:
                        print(f"Ingest failed {file_path}: {exc}")
            print(f"Ingested {total} chunks from {args.query}")
            return 0
        if args.action == "check":
            points = memory.list_points(args.collection, limit=50)
            print(f"{args.collection}: {len(points)} points")
            for point in points:
                print(f"  {point.id} {point.payload.get('text', '')[:80]}")
            return 0
        if args.action == "prune":
            result = memory.prune(args.collection) if not args.dry_run else {"pruned": 0, "dry_run": True}
            print(f"Prune {args.collection}: {result}")
            return 0
        if args.action == "list":
            print(f"Projects: {', '.join(memory.list_projects())}")
            return 0
        return 2
    if args.command == "skills":
        from ..capabilities.skill_lifecycle import LifecycleStore
        lifecycle = LifecycleStore(config.home)
        if args.action == "list":
            print(f"Skills: {list(lifecycle._data.keys())}")
            return 0
        if not args.name:
            print("skills show/check/rollback need a name"); return 2
        if args.action == "show":
            print(f"{args.name}: {lifecycle.state(args.name)}")
            return 0
        if args.action == "check":
            print(f"{args.name} governor ok: {lifecycle.governor_check(args.name)}")
            return 0
        if args.action == "rollback":
            lifecycle._data[args.name] = {"state": "candidate", "successes": 0, "failures": 0, "learns": {}}; lifecycle._save()
            print(f"{args.name} rolled back to candidate")
            return 0
        return 2
    if args.command == "tools":
        from ..capabilities.plugins import PluginRegistry
        registry = PluginRegistry(config.home); registry.load_all()
        if args.action == "list":
            print(f"Plugin tools: {', '.join(registry.tool_names())}")
            print("Sandbox tools: write, read, list, shell, calculate")
            return 0
        if args.action == "check":
            print(f"{args.name} exists: {args.name in registry.tool_names() or args.name in ('write', 'read', 'list', 'shell', 'calculate')}")
            return 0
        return 2
    if args.command == "dag":
        from ..agentic.dag_store import DagStore
        store = DagStore(config.home)
        if args.action == "list":
            for dag in store.list_dags():
                print(f"{dag['id']}: {dag['goal']} nodes={dag['nodes']}")
            return 0
        if args.action == "show":
            dag = store.get(args.id)
            print(dag if dag else f"No DAG {args.id}")
            return 0 if dag else 2
        return 2
    if args.command == "mission":
        from ..capabilities.mission import MissionStore
        import json as _json
        store = MissionStore(config.home)
        if args.action == "list":
            missions_dir = config.home / "missions"
            for path in sorted(missions_dir.glob("*/mission.json")) if missions_dir.exists() else []:
                try:
                    data = _json.loads(path.read_text())
                    print(f"{data.get('project_id')}: {data.get('status')}")
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
                # Old Aidon discard was explicit only for REJECTED drafts, here we require cancelled or rejected, otherwise refuse
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
        return 0
    if args.command == "schedule":
        from ..interface.triggers import Scheduler, SchedulerJob
        scheduler = Scheduler(config.home)
        if args.action == "list":
            for job in scheduler.jobs:
                print(f"{job.name}: {job.goal} schedule={job.schedule} project={job.project_id}")
            return 0
        if args.action == "show":
            for job in scheduler.jobs:
                if job.name == args.name:
                    print(job); return 0
            print(f"No job {args.name}"); return 2
        if args.action == "add":
            if not args.name or not args.goal:
                print("schedule add needs --name and --goal"); return 2
            scheduler.add_job(SchedulerJob(name=args.name, goal=args.goal, schedule=args.schedule, project_id=args.project))
            print(f"Added job {args.name}")
            return 0
        if args.action == "remove":
            before = len(scheduler.jobs)
            scheduler.jobs = [j for j in scheduler.jobs if j.name != args.name]
            scheduler.save()
            print(f"Removed {args.name}" if len(scheduler.jobs) < before else f"No job {args.name}")
            return 0 if len(scheduler.jobs) < before else 2
        return 2
    if args.command == "sandbox":
        from ..capabilities.execution import make_backend
        try:
            backend = make_backend(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
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
        print("Aidon chat, type a goal, empty line to exit. This is a terminal chat, not the WebUI, approval is asked on the console.")
        while True:
            try:
                line = input("You: ").strip()
            except EOFError:
                break
            if not line:
                break
            print(f"Aidon: received goal {line!r}, run it with goal command for a full sandbox run, chat here is a direct reply only, no pipeline, no approval gate for a chat reply.")
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
                        _srunner = GoalRunner(client, sandbox, _cli_gate(), config.home, memory=memory, project_id="default", plugins=_splugins, execution_backend=_sbackend, budget=_SB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_ST(config.home, project_id="default"), model_verifier=_SMV(client), kernel_mode=config.kernel_mode)
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
            runner = GoalRunner(client, sandbox, _cli_gate(), config.home, memory=memory, project_id=job.project_id, plugins=_plugins, execution_backend=_backend, budget=_B(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T(config.home, project_id=job.project_id), model_verifier=_MV(client), kernel_mode=config.kernel_mode)
            report = runner.run(job.goal)
            print(f"Tick job {job.name}: {report.status}, files: {', '.join(report.files)}")
        return 0
    if args.command == "repl":
        from ..interface.repl import Repl
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
            runner = GoalRunner(client, sandbox, _cli_gate(), config.home, memory=memory, project_id="default", plugins=_plugins, execution_backend=_backend, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, project_id="default"), model_verifier=_MV2(client), kernel_mode=config.kernel_mode)
            report = runner.run(goal)
            if report.status == "chat":
                return report.chat_reply or "Received."
            return f"[{report.status}] {', '.join(report.files)} {report.error}".strip()
        Repl(_repl_handler).run()
        return 0
    if args.command == "webui":
        from ..interface.messaging import MessagingConfig, send_slack, send_whatsapp
        from ..interface.triggers import TriggerDispatcher
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

        def slack_sender(channel: str, text: str, thread_ts=None):
            result = send_slack(msg, channel, text, thread_ts=thread_ts)
            if not result["ok"]:
                print(f"Slack send failed: {result['error']}")

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
                             transcribe_key=config.transcribe_api_key or config.api_key or None)

        def _goal_handler(event):
            from ..agentic.loop import GoalRunner
            from ..interface.run_control import new_run_id
            from ..agentic.budgets import Budget
            from ..agentic.trace import TraceRecorder
            from ..agentic.verifier_model import ModelVerifier
            from ..capabilities.plugins import PluginRegistry
            from ..agentic.semantic_router import SemanticRouter
            from ..interface.scheduler_loop import AutonomyTracker, NudgeTracker
            run_id = new_run_id()
            run_control.register(run_id, event.goal, event.project_id)
            # Full runner, same as the CLI goal run, not a thin runner:
            # budget, trace, model verifier, plugins, execution backend,
            # semantic router skip, and autonomy plus nudge trackers
            # on the WebUI gate, so a WebUI, WhatsApp, or Slack run
            # is the same run as a CLI run, with the same checks.
            server.webui_gate.nudge_tracker = NudgeTracker(
                nudge_minutes=config.approval_nudge_minutes,
                channel=config.approval_nudge_channel)
            server.webui_gate.autonomy_tracker = AutonomyTracker(
                enabled=config.autonomy_enabled, threshold=config.autonomy_threshold,
                decay_days=config.autonomy_decay_days)
            server.webui_gate.slack_sender = slack_sender
            plugins = PluginRegistry(config.home)
            plugins.load_all()
            budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                            max_seconds=config.budget_seconds)
            trace = TraceRecorder(config.home, project_id=event.project_id)
            try:
                from ..capabilities.execution import make_backend
                execution_backend = make_backend(
                    config.execution_backend, sandbox,
                    image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                execution_backend = None
            try:
                semantic_router = SemanticRouter(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
            except Exception:
                semantic_router = None
            try:
                from ..interface.preapproval import PreapprovalStore
                from ..interface.preapproval_gate import PreapprovalGate
                _gate = PreapprovalGate(server.webui_gate.gate, PreapprovalStore(config.home), event.project_id)
            except Exception:
                _gate = server.webui_gate.gate
            def _web_researcher(goal_text):
                try:
                    from ..capabilities.search import search as _s
                    _r = _s(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url)
                    return [f"{x.get('title', '')}: {x.get('snippet', '')}" for x in _r.get("results", [])]
                except Exception:
                    return []
            runner = GoalRunner(client, sandbox, _gate,
                                config.home, memory=memory,
                                project_id=event.project_id, plugins=plugins,
                                execution_backend=execution_backend, budget=budget,
                                trace=trace, model_verifier=ModelVerifier(client),
                                kernel_mode=config.kernel_mode,
                                semantic_router=semantic_router,
                                use_semantic_router_skip=True,
                                run_id=run_id, run_control=run_control,
                                researcher=_web_researcher, extra_tools={})
            # Swarm, Aidon decides: if the goal genuinely splits,
            # run the subgoals in parallel and merge, else single run.
            try:
                from ..agentic.swarm import run_swarm
                def _swarm_runner(subgoal):
                    sub_runner = GoalRunner(client, sandbox, server.webui_gate.gate,
                                            config.home, memory=memory,
                                            project_id=event.project_id, plugins=plugins,
                                            budget=budget, trace=trace,
                                            model_verifier=ModelVerifier(client),
                                            kernel_mode=config.kernel_mode,
                                            run_id=run_id, run_control=run_control)
                    sub_report = sub_runner.run(subgoal)
                    return f"[{sub_report.status}] {', '.join(sub_report.files)} {sub_report.error}".strip()
                swarm_result = run_swarm(event.goal, client, _swarm_runner)
                if swarm_result.get("swarmed"):
                    event.send_reply(f"[{run_id}] Swarm, subgoals: {', '.join(swarm_result['subgoals'])}\n{swarm_result['merged']}")
                    return
            except Exception:
                pass
            report = runner.run(event.goal)
            if report.status == "chat":
                event.send_reply(f"[{run_id}] {report.chat_reply or 'Received.'}")
            elif report.status == "done":
                event.send_reply(f"[{run_id}] Done: {', '.join(report.files)}")
            elif report.status == "rejected":
                event.send_reply(f"[{run_id}] Rejected, no live action taken.")
            elif report.status == "cancelled":
                event.send_reply(f"[{run_id}] Cancelled, no further steps run.")
            else:
                event.send_reply(f"[{run_id}] Failed: {report.error}")

        dispatcher.register("webui", _goal_handler)
        dispatcher.register("whatsapp", _goal_handler)
        dispatcher.register("slack", _goal_handler)
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
            _runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                                 memory=memory, project_id=goal_record.project_id, plugins=_plugins,
                                 execution_backend=_backend, budget=_VB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                 trace=_VT(config.home, project_id=goal_record.project_id), model_verifier=_VM(client),
                                 kernel_mode=config.kernel_mode)
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
        from ..interface.preapproval import PreapprovalStore
        import time as _time
        store = PreapprovalStore(config.home)
        if args.action == "preapprove":
            ok = store.grant(args.spec, args.kind, _time.time() + args.hours * 3600, reversible=True)
            print(f"Pre-approval {'granted' if ok else 'refused, irreversible or external email'}: {args.spec} {args.kind}")
            return 0 if ok else 2
        if args.action == "morning":
            for item in store.morning_report():
                print(f"{item['spec_id']} {item['action_kind']}: undo: {item['undo']}")
            return 0
        # serve: poll due jobs every 60 seconds, run each via GoalRunner.
        from datetime import datetime
        from ..interface.triggers import Scheduler
        print("Scheduler daemon, polling every 60 seconds, Ctrl-C to stop.")
        print("Scheduler daemon also watches ingest and polls Slack, when configured, same loop.")
        def _full_scheduler_runner():
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
            return GoalRunner(client, sandbox, _cli_gate(), config.home, memory=memory, project_id="default", plugins=_pl, execution_backend=_be, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, project_id="default"), model_verifier=_M2(client), kernel_mode=config.kernel_mode)

        import os as _os
        _slack_last_ts = ""
        _slack_ts_path = config.home / "slack_last_ts.txt"
        if _slack_ts_path.is_file():
            _slack_last_ts = _slack_ts_path.read_text().strip()
        while True:
            scheduler = Scheduler(config.home)
            for job in scheduler.check_due(datetime.now()):
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
                runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                                    memory=memory, project_id="default", plugins=_plugins,
                                    execution_backend=_backend, budget=_Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                    trace=_Trace(config.home, project_id="default"), model_verifier=_MV(client),
                                    kernel_mode=config.kernel_mode, semantic_router=_router2,
                                    use_semantic_router_skip=True)
                report = runner.run(job.goal)
                print(f"Job {job.name}: {report.status}, files: {', '.join(report.files)}")
            # Ingest watcher, same loop, same GoalRunner, no separate daemon.
            try:
                from ..interface.triggers import IngestWatcher
                watcher = IngestWatcher(config.ingest_path, config.home)
                for ingest_event in watcher.events(project_id="default"):
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
                        _poll_runner = _full_scheduler_runner()
                        _poll_report = _poll_runner.run(_message["text"])
                        print(f"Slack poll job: {_poll_report.status}")
            except Exception as exc:
                print(f"Slack poll error: {exc}")
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
            image=config.docker_image, allow_network=config.docker_network)
    except Exception as exc:
        print(f"execution backend error: {exc}")
        return 2
    if args.command == "plugin":
        from pathlib import Path
        from ..capabilities.plugins import PluginRegistry, scan_plugin
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
        from ..capabilities.plugins import PluginRegistry
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
    from ..capabilities.plugins import PluginRegistry
    plugins = PluginRegistry(config.home)
    plugins.load_all()
    from ..agentic.budgets import Budget
    from ..agentic.trace import TraceRecorder
    budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                    max_seconds=config.budget_seconds)
    trace = TraceRecorder(config.home, project_id=args.project)
    from ..agentic.verifier_model import ModelVerifier
    model_verifier = ModelVerifier(client)
    try:
        from ..agentic.semantic_router import SemanticRouter as _SR
        _cli_router = _SR(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
    except Exception:
        _cli_router = None
    def _researcher(goal_text):
        try:
            from ..capabilities.search import search as _search
            result = _search(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url)
            return [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in result.get("results", [])]
        except Exception:
            return []
    runner = GoalRunner(client, sandbox, _cli_gate(), config.home,
                        memory=memory, promotion_ask=_promotion_ask,
                        project_id=args.project, plugins=plugins, execution_backend=execution_backend,
                        budget=budget, trace=trace, model_verifier=model_verifier,
                        kernel_mode=config.kernel_mode, semantic_router=_cli_router,
                        use_semantic_router_skip=True, researcher=_researcher, extra_tools={})
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
                sub_report = runner.run(subgoal, spec=spec)
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
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
