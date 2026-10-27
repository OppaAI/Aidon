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


def _is_transient(exc: BaseException) -> bool:
    return "transient" in str(exc).lower()


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

    def _get_checkpoint(self, node_id: str) -> str | None:
        key = self._checkpoint_key(node_id)
        if self.state is not None:
            value = self.state.get_state(self.run_id, key)
            return value if value != "" else None
        return self._memory.get(key)

    def _set_checkpoint(self, node_id: str, result: str) -> None:
        key = self._checkpoint_key(node_id)
        if self.state is not None:
            self.state.save_state(self.run_id, key, result)
        else:
            self._memory[key] = result

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

    def resume(self) -> dict:
        """Continue a killed run: execute() already skips checkpoints."""
        return self.execute()
