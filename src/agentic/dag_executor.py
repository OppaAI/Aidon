"""DAG executor with crash-resume checkpoints.

A DAG is a plan, not a run. This executor is the run: it walks the
nodes in topological order, runs each one through a caller-supplied
callable, and checkpoints every completed node's result before moving
on. A run killed mid-way can be resumed from the checkpoint state:
completed nodes are skipped, never re-executed, so a resume continues
where the run died instead of starting over.

Checkpoint state is the authority, not the filesystem: a node whose
deliverable file happens to exist is NOT skipped unless its result is
checkpointed. That rule is deliberate — a file can be stale, partial,
or written by another run, while a checkpoint means this run's node
actually completed.

State is any object with save_state(run_id, key, value) and
get_state(run_id, key) methods (the Registry state KV is the usual
one); when state is None, an in-memory dict backs the checkpoints,
so the executor still works in tests and in single-process runs.
Checkpoint keys are f"node:{node.id}", values are the node's result
string.

Failures:
- A transient failure — an exception whose message contains
  "transient" — retries the node once, then fails the run if the
  retry also fails.
- A real failure stops the run but preserves everything that did
  complete: results and checkpoints for completed nodes stay, and
  the returned dict carries them plus the failure. Nothing completed
  is rolled back or forgotten.
"""

from __future__ import annotations

import hashlib
import json


def _is_transient(exc: BaseException) -> bool:
    return "transient" in str(exc).lower()


def resolve_parallel_workers(max_workers=None) -> int:
    """Resolve the parallel-worker count: explicit arg, else env, else 3.

    The single knob for both parallel executors (the loop's
    level driver and DAGExecutor): AIDON_PARALLEL_WORKERS in
    .env, the settings page, and .env.example. An invalid
    value falls back to 3, never to 0 or negative, so a typo
    cannot serialize or crash a run.
    """
    if max_workers is not None:
        try:
            return max(1, int(max_workers))
        except (TypeError, ValueError):
            pass
    try:
        import os
        return max(1, int(os.environ.get("AIDON_PARALLEL_WORKERS", "3") or "3"))
    except ValueError:
        return 3


class DAGExecutor:
    """Run a DAG's nodes in order, checkpointing each completion."""

    def __init__(self, dag, run_node, state=None, run_id: str = ""):
        self.dag = dag
        self.run_node = run_node
        self.state = state
        self.run_id = str(run_id or "")
        self._memory: dict[str, str] = {}

    # -- checkpoint state ------------------------------------------

    def _checkpoint_key(self, node_id: str) -> str:
        return f"node:{node_id}"

    def _dag_fingerprint(self) -> str:
        nodes = []
        for node in self.dag.topological_order():
            nodes.append({
                "id": node.id,
                "tool": node.tool,
                "args": node.args,
                "deliverable": node.deliverable,
                "depends_on": list(node.depends_on or []),
            })
        canonical = json.dumps(nodes, sort_keys=True,
                               ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _ensure_identity(self) -> None:
        key = "__dag_fingerprint__"
        expected = self._dag_fingerprint()
        stored = (self.state.get_state(self.run_id, key)
                  if self.state is not None else self._memory.get(key))
        if stored and stored != expected:
            self._clear_all_checkpoints()
        elif not stored and any(
                self._get_checkpoint(node.id) is not None
                for node in self.dag.topological_order()):
            # Checkpoints without an identity cannot be trusted.
            self._clear_all_checkpoints()
        if self.state is not None:
            self.state.save_state(self.run_id, key, expected)
        else:
            self._memory[key] = expected

    def _get_checkpoint(self, node_id: str) -> str | None:
        key = self._checkpoint_key(node_id)
        if self.state is not None:
            value = self.state.get_state(self.run_id, key)
            return value if value != "" else None
        return self._memory.get(key)

    def _set_checkpoint(self, node_id: str, result: str) -> None:
        identity_key = "__dag_fingerprint__"
        if self.state is not None:
            if not self.state.get_state(self.run_id, identity_key):
                self.state.save_state(self.run_id, identity_key,
                                      self._dag_fingerprint())
        elif identity_key not in self._memory:
            self._memory[identity_key] = self._dag_fingerprint()
        key = self._checkpoint_key(node_id)
        if self.state is not None:
            self.state.save_state(self.run_id, key, result)
        else:
            self._memory[key] = result

    def _clear_checkpoint(self, node_id: str) -> None:
        key = self._checkpoint_key(node_id)
        if self.state is not None:
            self.state.save_state(self.run_id, key, "")
        else:
            self._memory.pop(key, None)

    def _clear_all_checkpoints(self) -> None:
        if self.state is not None and hasattr(self.state, "clear_state_prefix"):
            self.state.clear_state_prefix(self.run_id, "node:")
        elif self.state is not None:
            for node in self.dag.topological_order():
                self._clear_checkpoint(node.id)
        else:
            for key in list(self._memory):
                if key.startswith("node:"):
                    self._memory.pop(key, None)

    def partial_results(self) -> dict[str, str]:
        """Checkpointed results so far, in topological order."""
        results: dict[str, str] = {}
        for node in self.dag.topological_order():
            value = self._get_checkpoint(node.id)
            if value is not None:
                results[node.id] = value
        return results

    # -- execution -------------------------------------------------

    def execute(self) -> dict:
        """Run all nodes, skipping checkpointed ones, checkpointing each win.

        Returns {"status": "done" | "failed", "results": {...},
        "completed": [...], "failed": {id: error} | None}. On failure,
        "results" and "completed" still carry everything that finished
        before the failing node, in topological order.
        """
        self._ensure_identity()
        results = self.partial_results()
        completed = list(results)
        failed = None
        for node in self.dag.topological_order():
            if node.id in results:
                continue
            try:
                result = self.run_node(node)
            except Exception as exc:
                if _is_transient(exc):
                    try:
                        result = self.run_node(node)
                    except Exception as retry_exc:
                        failed = {node.id: str(retry_exc)}
                        break
                else:
                    failed = {node.id: str(exc)}
                    break
            result = str(result)
            self._set_checkpoint(node.id, result)
            results[node.id] = result
            completed.append(node.id)
        return {
            "status": "failed" if failed is not None else "done",
            "results": results,
            "completed": completed,
            "failed": failed,
        }

    def execute_parallel(self, max_workers: int | None = None) -> dict:
        """Run a DAG level by level, nodes in one level concurrently.

        Levels come from compute_levels: a level runs only after
        every earlier level finished, so a node never runs before
        its dependencies, and independent nodes share a level and
        run together. Workers resolve via resolve_parallel_workers
        (explicit arg, else AIDON_PARALLEL_WORKERS, else 3). A
        failure in a level stops the run — later levels never
        start — while completed nodes and their checkpoints are
        preserved, exactly like the sequential execute().
        Checkpoints are written under a lock, so concurrent
        completions cannot interleave.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        from .dag import compute_levels

        self._ensure_identity()
        results = self.partial_results()
        completed = list(results)
        failed = None
        lock = threading.Lock()
        try:
            levels = compute_levels([(node.id, list(node.depends_on or []))
                                     for node in self.dag.nodes.values()])
        except ValueError as exc:
            return {"status": "failed", "results": results, "completed": completed,
                    "failed": {"__schedule__": str(exc)}}
        by_id = dict(self.dag.nodes)
        workers = resolve_parallel_workers(max_workers)

        def _run(node_id):
            try:
                return node_id, str(self.run_node(by_id[node_id])), None
            except Exception as exc:
                if _is_transient(exc):
                    try:
                        return node_id, str(self.run_node(by_id[node_id])), None
                    except Exception as retry_exc:
                        return node_id, None, str(retry_exc)
                return node_id, None, str(exc)

        for level in levels:
            pending = [node_id for node_id in level if node_id not in results]
            if not pending:
                continue
            with ThreadPoolExecutor(max_workers=min(workers, len(pending))) as pool:
                outcomes = list(pool.map(_run, pending))
            for node_id, result, error in outcomes:
                if error is not None:
                    failed = {node_id: error}
                else:
                    with lock:
                        self._set_checkpoint(node_id, result)
                    results[node_id] = result
                    completed.append(node_id)
            if failed is not None:
                break
        return {"status": "failed" if failed is not None else "done",
                "results": results, "completed": completed, "failed": failed}

    def resume(self) -> dict:
        """Continue a killed run: execute() already skips checkpoints."""
        return self.execute()
