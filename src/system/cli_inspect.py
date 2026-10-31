"""Inspection and maintenance CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

from .runtime import _cli_gate, _make_memory, _network_available
from ..capabilities.sandbox import Sandbox


def run_inspection_command(args, config, sandbox, memory):
    """Run a non-interactive inspection/maintenance command, if applicable."""
    if args.command == "learn":
        from ..agentic.learn_cycle import run_learn_cycle
        from ..capabilities.skill_lifecycle import LifecycleStore
        summary = run_learn_cycle(
            config.home, sandbox,
            governor_check=LifecycleStore(config.home).governor_check,
            max_per_day=max(0, config.learn_max_skills_per_day))
        print(f"Learn cycle: promoted={summary.get('promoted', [])} "
              f"retired={summary.get('retired_duplicates', [])} "
              f"trusted_for_review={summary.get('trusted_duplicates_for_review', [])} "
              f"skipped={summary.get('skipped', [])}")
        return 0
    if args.command == "resume":
        from ..agentic.resume import list_runs, resume_state
        if args.run_id:
            ok, state, error = resume_state(config.home, args.run_id)
            if not ok:
                print(f"cannot resume: {error}"); return 2
            print(f"Resuming run {args.run_id}: goal={state.get('goal','')} steps={len(state.get('steps',[]))} completed={state.get('completed',[])}")
        else:
            for run_id in list_runs(config.home):
                print(run_id)
        return 0
    if args.command == "plan-guard":
        import json as _json
        from pathlib import Path as _Path
        from ..agentic.plan_guard import plan_first_summary, validate_plan
        plan = _json.loads(_Path(args.file).read_text())
        failures = validate_plan(plan, [t.strip() for t in args.tools.split(",") if t.strip()])
        print(plan_first_summary(plan))
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}")
            return 2
        print("plan guard: pass")
        return 0
    if args.command == "inventory":
        from pathlib import Path as _Path
        from ..agentic.artifact_inventory import inventory_markdown
        workdir = _Path(args.workdir) if args.workdir else config.workdir
        print(inventory_markdown(workdir))
        return 0
    if args.command == "delivery":
        import json as _json
        from pathlib import Path as _Path
        from ..agentic.delivery_summary import delivery_summary
        report = _json.loads(_Path(args.file).read_text())
        print(delivery_summary(report))
        return 0
    if args.command == "think":
        from ..agentic.think_effort import resolve_effort, sampling_for
        level = resolve_effort(args.phase, requested=args.level or None, escalate=args.escalate)
        print(f"phase={args.phase} effort={level} sampling={sampling_for(args.phase)}")
        return 0
    if args.command == "ingest":
        from ..memory.ingest import ingest_folder, remove_folder
        from pathlib import Path as _P
        if args.action == "remove":
            print(f"Removed {remove_folder(config.home, args.folder)} points")
        else:
            print(ingest_folder(config.home, args.folder, dry_run=(args.action == "dry-run")))
        return 0
    if args.command == "metrics":
        from ..agentic.autonomy_metrics import AutonomyMetrics
        metrics = AutonomyMetrics(config.home)
        print(metrics.render(args.days) if args.render else metrics.summary(args.days))
        return 0
    if args.command == "vrm-check":
        from pathlib import Path as _P
        from ..capabilities.vrm_check import check_vrm
        vrm_path = _P(args.vrm) if args.vrm else config.home / "avatar.vrm"
        if args.js:
            js_source = _P(args.js).read_text()
        else:
            from ..capabilities.audio import vrm_lip_sync_js
            js_source = vrm_lip_sync_js()
        result = check_vrm(vrm_path, js_source)
        print(result)
        return 0 if result.get("ok") else 2
    if args.command == "skill-library":
        from ..capabilities.skill_library import SkillLibrary, import_skill_file
        if args.action == "import":
            if not args.file:
                print("skill-library import needs --file"); return 2
            try:
                skill = import_skill_file(config.home, args.file,
                                          args.name or None)
            except (ValueError, PermissionError) as exc:
                print(f"import failed: {exc}"); return 1
            print(f"Imported skill {skill.name} as candidate "
                  f"from {args.file}"); return 0
        lib = SkillLibrary(config.home)
        if args.action == "list":
            for skill in lib.list():
                print(f"{skill.name} v{skill.version} {skill.state} privileged={skill.privileged}")
            return 0
        if not args.name:
            print("skill-library show/promote/trust/retire/rollback/versions/from-experience need a name"); return 2
        if args.action == "from-experience":
            from ..memory.experience import (
                promote_experience_to_skill, recall_experiences)
            memory = _make_memory(config)
            matches = recall_experiences(memory, args.name, limit=1)
            if not matches:
                print(f"No successful experience matches {args.name!r}"); return 2
            try:
                path = promote_experience_to_skill(config.home, matches[0])
            except FileExistsError as exc:
                print(str(exc)); return 1
            print(f"Promoted {matches[0].goal!r} to skill: {path}"); return 0
        if args.action == "show":
            skill = lib.get(args.name)
            print(skill if skill else f"No skill {args.name}"); return 0 if skill else 2
        if args.action == "promote":
            import tempfile
            skill = lib.get(args.name)
            if skill is None:
                print(f"No skill named {args.name}"); return 2
            with tempfile.TemporaryDirectory(prefix="aidon-skill-tests-") as test_dir:
                test_sandbox = Sandbox(
                    Path(test_dir), config.sandbox_timeout_seconds,
                    mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
                    fsize_mb=config.sandbox_fsize_mb,
                    cpu_seconds=config.sandbox_cpu_seconds,
                    backend=config.sandbox_backend, allow_network=False,
                    require_isolation=True)
                report = lib.run_tests(skill.name, test_sandbox)
            if not report.get("passed"):
                print(f"Skill tests did not pass: {report.get('note', report.get('results', []))}")
                return 1
            print(lib.promote(args.name, report)); return 0
        if args.action == "trust":
            skill = lib.get(args.name)
            if skill is None:
                print(f"No skill {args.name}"); return 2
            if skill.state != "validated":
                print(f"trust refused: {args.name} must pass skill-library promote first")
                return 1
            action_class = "skill_privileged" if skill.privileged else "skill"
            gate = _cli_gate(config.home)
            decision = gate.ask(
                "skill",
                f"Trust skill {args.name}?",
                f"Promote {args.name} (v{skill.version}) to trusted.\n"
                f"{skill.description or '(no description)'}",
                action_class=action_class,
            )
            if decision.verdict not in ("approved", "auto_approved", "edited"):
                print(f"trust rejected: {decision.verdict}"); return 1
            print(lib.trust(args.name, approved=True)); return 0
        if args.action == "retire":
            print(lib.retire(args.name, "retired from CLI")); return 0
        if args.action == "rollback":
            print(lib.rollback(args.name, args.version)); return 0
        if args.action == "versions":
            print(lib.version_history(args.name)); return 0
        return 2
    if args.command == "conversations":
        from ..memory.conversations_db import ConversationStoreDB
        store = ConversationStoreDB(config.home)
        if args.action == "list":
            print(store.list_conversations("default")); return 0
        if args.action == "import":
            import json as _json
            from pathlib import Path as _P
            count = 0
            for path in sorted((_P(config.home) / "conversations").glob("*.json")):
                try:
                    data = _json.loads(path.read_text())
                except Exception:
                    continue
                store.import_conversation(str(data.get("id", path.stem)),
                                          list(data.get("turns", [])))
                count += 1
            print(f"Imported {count} legacy conversations"); return 0
        if not args.id:
            print("conversations history/record/delete need an id"); return 2
        if args.action == "history":
            print(store.history(args.id)); return 0
        if args.action == "record":
            print(store.record_exchange(args.id, args.role, args.text)); return 0
        if args.action == "delete":
            print(f"Deleted {args.id}: {store.delete_conversation(args.id)}"); return 0
        return 2
    if args.command == "registry-db":
        from ..capabilities.registry_db import Registry
        reg = Registry(config.home)
        if args.action == "runs":
            print(reg.list_runs()); return 0
        if args.action == "show":
            print(reg.get_run(args.id) if args.id else "registry-db show needs an id"); return 0
        if args.action == "skill-versions":
            print(reg.list_skill_versions(args.id) if args.id else "registry-db skill-versions needs a skill name"); return 0
        return 2
    if args.command == "hub":
        import os as _os
        from ..capabilities.remote_hub_server import HubClient, HubServer
        if args.action == "serve":
            token = args.token or _os.environ.get("AIDON_HUB_TOKEN", "")
            server = HubServer(config.home, token=token)
            print(f"Hub serving on 127.0.0.1:{args.port} (token set: {bool(token)})")
            server.serve(port=args.port)
            return 0
        client = HubClient(args.url, token=args.token or _os.environ.get("AIDON_HUB_TOKEN", ""))
        if args.action == "list":
            print(client.list_dags()); return 0
        if args.action == "push":
            import json as _json
            from pathlib import Path as _P
            print(client.push_dag(_json.loads(_P(args.path).read_text()))); return 0
        if args.action == "pull":
            print(client.pull_dag(args.id)); return 0
        return 2
    if args.command == "mcp":
        import os as _os, json as _json
        from ..interface.mcp_http import McpHttpClient
        url = args.url or _os.environ.get("AIDON_MCP_URL", "")
        if not url:
            print("mcp needs --url or AIDON_MCP_URL"); return 2
        client = McpHttpClient(url, token=_os.environ.get("AIDON_MCP_TOKEN", ""))
        client.initialize()
        try:
            if args.action == "list":
                print(client.list_tools()); return 0
            print(client.call_tool(args.name, _json.loads(args.args))); return 0
        finally:
            client.close()
    if args.command == "docstore":
        from ..memory.docstore import DocStore
        from pathlib import Path as _P
        store = DocStore.from_qdrant(config.home) if getattr(args, "qdrant", False) else DocStore(home=config.home)
        if args.action == "ingest":
            path = _P(args.arg)
            print(store.ingest(path.name, path.read_text(encoding="utf-8", errors="replace"))); return 0
        if args.action == "search":
            print(store.search_all(args.arg)); return 0
        if args.action == "drop":
            print(f"Dropped {args.arg}: {store.drop(args.arg)}"); return 0
        if args.action == "list":
            print(store.collections()); return 0
        return 2
    if args.command == "intake":
        from ..agentic.intake_schema import get_schema, missing_slots, ready, summary_lines
        schema = get_schema(args.schema)
        if schema is None:
            print(f"Unknown intake schema: {args.schema}"); return 2
        spec = {"goal": args.text}
        print(f"Ready: {ready(schema, spec)}")
        print(f"Missing: {missing_slots(schema, spec)}")
        for line in summary_lines(schema, spec):
            print(line)
        return 0
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
            from ..memory.conversations_db import ConversationStoreDB
            ids = [r["id"] for r in ConversationStoreDB(config.home).list_all_conversations()]
            print(f"Chats: {', '.join(ids)}")
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
    if args.command == "diagnostics":
        from .diagnostics import build_diagnostic_report, write_diagnostic_report
        report = build_diagnostic_report(config.home, args.run_id)
        path = write_diagnostic_report(config.home, args.run_id)
        print(report)
        print(f"\nSaved to {path}")
        return 0
    if args.command == "tools":
        from ..capabilities.plugins import PluginRegistry
        from ..capabilities.tool_catalog import all_tool_names
        registry = PluginRegistry(config.home); registry.load_all()
        available = all_tool_names(
            registry.tool_names(),
            selfmod=(config.tool_posture == "selfmod"
                     and not config.kernel_mode))
        if args.action == "list":
            print(f"Plugin tools: {', '.join(registry.tool_names())}")
            print("Goal tools: " + ", ".join(available))
            return 0
        if args.action == "check":
            print(f"{args.name} exists: {args.name in available}")
            return 0
        return 2
    if args.command == "capabilities":
        from ..capabilities.capability_registry import CapabilityRegistry
        registry = CapabilityRegistry(config.home)
        if args.action == "list":
            for entry in registry.list():
                print(f"{entry.get('kind')}:{entry.get('name')} {entry.get('status')} uses={entry.get('use_count', 0)} last_used={entry.get('last_used', '') or 'never'}")
            return 0
        if args.action == "stale":
            stale = registry.stale_entries(days=args.days)
            for entry in stale:
                print(f"{entry['kind']}:{entry['name']} {entry['status']} idle={entry['idle_days']}d uses={entry['use_count']}")
            if not stale:
                print("No stale capabilities.")
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
        if args.action == "run":
            # Execute a saved DAG's tool nodes via the DAG executor.
            # Tool nodes map to the core ToolRegistry (write/read/list/
            # shell/calculate, confined to the sandbox workdir); fast
            # and strong nodes have no tool mapping, so a DAG containing
            # any is refused before anything runs rather than half-run.
            # Checkpoints live in the Registry under the run id, so a
            # killed run resumes by re-running with the same --run-id.
            from ..agentic.dag import DAG, DAGNode
            from ..agentic.dag_executor import DAGExecutor
            from ..capabilities.registry_db import Registry
            from ..capabilities.tools import ToolRegistry
            import uuid as _dag_uuid
            data = store.get(args.id)
            if data is None:
                print(f"No DAG {args.id}")
                return 2
            try:
                dag = DAG([DAGNode.from_dict(node) for node in data.get("nodes", [])])
                dag.validate_strict()
            except Exception as exc:
                print(f"dag run {args.id} refused: {exc}")
                return 2
            try:
                # Learned absolute paths are made relative to this
                # run's workdir first, the same relativize() the
                # loop applies to adapted DAGs, so a DAG learned
                # in this workdir passes ToolRegistry confinement
                # instead of failing on its own absolute paths.
                dag = dag.relativize(sandbox.workdir)
                non_tool = [node.id for node in dag.nodes.values() if node.executor != "tool"]
                if non_tool:
                    print(f"dag run supports tool nodes only, refusing DAG with non-tool nodes: {', '.join(non_tool)}")
                    return 2
                registry = ToolRegistry(sandbox.workdir, sandbox=sandbox,
                                        network_available=_network_available(None, sandbox))
                def _run_node(node):
                    args_dict = node.args if isinstance(node.args, dict) else {}
                    return registry.call(node.tool, args_dict)
                run_id = args.run_id or _dag_uuid.uuid4().hex[:12]
                executor = DAGExecutor(dag, _run_node, state=Registry(config.home), run_id=run_id)
                result = executor.execute_parallel() if getattr(args, "parallel", False) else executor.execute()
            except Exception as exc:
                print(f"dag run {args.id} failed: {exc}")
                return 1
            print(f"dag run {args.id}: {result['status']}, completed: {', '.join(result['completed'])}")
            if result["failed"]:
                print(f"failed: {result['failed']}")
            return 0 if result["status"] == "done" else 1
        return 2
    return None
