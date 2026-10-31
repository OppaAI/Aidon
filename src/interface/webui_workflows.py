"""Workflow-oriented WebUI endpoints: DAGs, replay and skill provenance."""

import json


class WebUIWorkflowMixin:
    """Keep workflow persistence and provenance separate from HTTP/chat code."""

    def handle_skill_source(self, run_id: str, conversation_id: str) -> dict | None:
        """Expose only a successful workflow owned by this conversation."""
        run_id = str(run_id or "")
        if (not run_id or len(run_id) > 128
                or not run_id.replace("-", "").isalnum()):
            return None
        path = self.home / "learned" / "runs" / f"{run_id}.json"
        try:
            if path.stat().st_size > 1024 * 1024:
                return None
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if (record.get("status") != "done"
                or str(record.get("run_id", "")) != run_id
                or str(record.get("conversation_id", "")) != str(conversation_id)):
            return None
        steps = record.get("workflow")
        if not isinstance(steps, list) or not steps:
            return None
        return {"run_id": run_id, "conversation_id": str(conversation_id),
                "goal": str(record.get("goal", ""))[:4000],
                "deliverables": [str(x)[:300] for x in
                                 (record.get("deliverables") or [])[:100]],
                "workflow": [{
                    "id": str(node.get("id", ""))[:100],
                    "name": str(node.get("name", ""))[:300],
                    "tool": str(node.get("tool", ""))[:100],
                    "deliverable": str(node.get("deliverable", ""))[:300],
                    "depends_on": [str(x)[:100] for x in
                                   (node.get("depends_on") or [])[:100]],
                    "expected": str(node.get("expected", ""))[:1000],
                } for node in steps[:100] if isinstance(node, dict)]}

    def factory_stages(self) -> list[dict]:
        """Read trace files and map kinds to factory stages, for /factory."""
        stages = []
        trace_dir = self.home / "trace"
        if not trace_dir.exists():
            return stages
        for path in sorted(trace_dir.glob("*.jsonl")):
            for line in path.read_text(errors="ignore").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                stages.append({"run": path.stem, "kind": event.get("kind", ""),
                               "status": event.get("status", "")})
        return stages

    def dag_store(self):
        from ..agentic.dag_store import DagStore
        return DagStore(self.home)

    def handle_dags_list(self) -> dict:
        return {"dags": self.dag_store().list_dags()}

    def handle_dag_get(self, dag_id: str) -> dict:
        data = self.dag_store().get(dag_id)
        return data if data is not None else {"error": "not found"}

    def handle_dag_save(self, body: dict) -> tuple[int, dict]:
        ok, failures = self.dag_store().save(
            str(body.get("id", "")), str(body.get("goal", "")),
            list(body.get("nodes", [])), memory=self.memory,
            merge=bool(body.get("merge", False)),
            layout=body.get("layout", {}))
        # Memory DAGs are also editable: if the editor asks for a
        # memory DAG, it is loaded via /api/memory-dags below, and
        # saved back through this same save, with merge, so the
        # editor no longer edits only the file store.
        return (200, {"ok": True}) if ok else (400, {"ok": False, "failures": failures})

    def handle_dag_delete(self, dag_id: str) -> dict:
        return {"ok": self.dag_store().delete(dag_id)}

    def replay_runs(self) -> list[str]:
        trace_dir = self.home / "trace"
        if not trace_dir.exists():
            return []
        return sorted(path.stem for path in trace_dir.glob("*.jsonl"))

    def replay_events(self, run: str) -> list[dict]:
        path = self.home / "trace" / f"{run}.jsonl"
        if not path.exists():
            return []
        events = []
        for line in path.read_text(errors="ignore").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def handle_dag_layout(self, body: dict) -> tuple[int, dict]:
        """Layered layout for a DAG, owned by the server so the
        editor and any caller agree on where a node goes.

        Body: {"dag": {"nodes": [...]}} (a bare list of nodes, or a
        dag dict with "steps" instead of "nodes", is also accepted).
        layer = longest-path depth from roots via depends_on
        (roots layer 0); x = layer * 200, y = index within the
        layer * 90. Entries follow topological order. An invalid
        or missing dag, an empty node id, an unknown dependency,
        or a cycle is 400, never a half-layout.
        """
        from ..agentic.dag import DAG, DAGNode
        dag_data = body.get("dag", {}) if isinstance(body, dict) else {}
        if isinstance(dag_data, list):
            raw_nodes = dag_data
        elif isinstance(dag_data, dict):
            raw_nodes = dag_data.get("nodes", dag_data.get("steps"))
        else:
            raw_nodes = None
        if not isinstance(raw_nodes, list) or not raw_nodes:
            return 400, {"error": "dag object with nodes required"}
        nodes = []
        for item in raw_nodes:
            if not isinstance(item, dict) or not str(item.get("id", "")):
                return 400, {"error": "every node needs a non-empty id"}
            try:
                nodes.append(DAGNode.from_dict(item))
            except Exception as exc:
                return 400, {"error": f"invalid node: {exc}"}
        dag = DAG(nodes)
        failures = dag.validate()
        if failures:
            return 400, {"error": "; ".join(failures)[:500]}
        layers: dict[str, int] = {}
        for node in dag.topological_order():
            deps = [d for d in node.depends_on if d in layers]
            layers[node.id] = (1 + max(layers[d] for d in deps)) if deps else 0
        per_layer: dict[int, int] = {}
        layout = []
        for node in dag.topological_order():
            layer = layers[node.id]
            index = per_layer.get(layer, 0)
            per_layer[layer] = index + 1
            layout.append({"id": node.id, "x": layer * 200,
                           "y": index * 90, "layer": layer})
        return 200, {"layout": layout}
