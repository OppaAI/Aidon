"""Modal worker pool: one isolated sandbox per DAG branch.

Today's level parallelism runs the steps of one level
together in a thread pool, but in a single shared
workspace: sibling steps can see each other's files even
without a declared dependency. That is convenient and it
is also a leak: a branch can read, overwrite, or depend
on a file it never declared.

The pool gives each branch its own worker: its own
ModalBackend (its own cloud sandbox, its own /work),
its own local workspace directory, and its own result.
Workers run concurrently in a thread pool (the same
mechanism the level runner already uses), and their
outputs combine only in an explicit integration step.

Flow:
  pool.spawn("research") -> local workspace path
  pool.spawn("backend")
  pool.run_all([("research", ["sh", "-c", "..."]),
                ("backend", ["sh", "-c", "..."])])
  pool.merge_into([research_ws, backend_ws], integration_ws)
  integration_backend.run([...tests...], integration_ws)
  pool.cleanup()

Merge semantics are strict on purpose: every worker
workspace is scanned first, and if two workers produced
the same relative path, the merge raises listing every
collision instead of silently overwriting. A half-merged
integration directory is worse than a loud failure, so
nothing is copied when a collision exists.

What the pool is not: it does not know DAG semantics,
and it is not wired into the level runner. Wiring levels
to isolated workers would change what sibling steps can
see (today: everything in the shared workspace), which
is a behavior change for existing plans, not a drop-in
optimization. The pool is the component; the wiring is
a separate decision.

Cost guard: max_workers caps how many sandboxes one
pool can create (Modal bills per sandbox). Spawning
past the cap raises instead of silently queuing.
"""

from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class ModalWorkerPool:
    """Owns a set of named Modal workers and their workspaces."""

    def __init__(self, root, app_name: str = "aidon-sandbox",
                 allow_network: bool = False, max_workers: int = 4,
                 backend_factory=None):
        self.root = Path(root)
        self.app_name = app_name
        self.allow_network = allow_network
        self.max_workers = max_workers
        self._backend_factory = backend_factory
        self._workers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.cleanup()
        return False

    def spawn(self, name: str) -> Path:
        """Create one worker with its own local workspace."""
        if name in self._workers:
            raise ValueError(f"worker already exists: {name}")
        if len(self._workers) >= self.max_workers:
            raise RuntimeError(
                f"worker cap reached ({self.max_workers}); "
                "cleanup or raise max_workers")
        workspace = self.root / name
        workspace.mkdir(parents=True, exist_ok=True)
        if self._backend_factory is not None:
            backend = self._backend_factory(name)
        else:
            from .execution import ModalBackend
            backend = ModalBackend(app_name=self.app_name,
                                   allow_network=self.allow_network)
        self._workers[name] = {"backend": backend,
                               "workspace": workspace}
        return workspace

    def run(self, name: str, command, timeout_seconds: int = 120):
        """Run one command in one worker's sandbox, syncing
        that worker's workspace up and back."""
        worker = self._workers.get(name)
        if worker is None:
            raise ValueError(f"unknown worker: {name}")
        return worker["backend"].run(command, worker["workspace"],
                                     timeout_seconds=timeout_seconds)

    def run_all(self, jobs, timeout_seconds: int = 120) -> dict:
        """Run jobs concurrently: [(worker_name, command), ...].

        Returns {name: ExecutionResult}. A worker's failure
        is carried in its result, not raised: one branch
        failing must not hide the other branches' results.
        Unknown worker names raise before anything runs.
        """
        for name, _command in jobs:
            if name not in self._workers:
                raise ValueError(f"unknown worker: {name}")
        results = {}

        def _one(job):
            name, command = job
            return name, self.run(name, command,
                                  timeout_seconds=timeout_seconds)

        with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
            for name, result in ex.map(_one, jobs):
                results[name] = result
        return results

    def merge_into(self, workspaces, target) -> list:
        """Copy every file from each workspace into target.

        Returns the merged relative paths. Raises listing
        every collision before copying anything: two
        workers producing the same path is a branch
        conflict, not a last-writer-wins.
        """
        target = Path(target)
        planned = {}
        collisions = []
        for workspace in workspaces:
            workspace = Path(workspace)
            for path in sorted(workspace.rglob("*")):
                if not path.is_file():
                    continue
                rel = path.relative_to(workspace)
                if rel in planned and planned[rel] != path:
                    collisions.append(str(rel))
                else:
                    planned[rel] = path
        if collisions:
            raise ValueError(
                "merge collision: workers produced the same paths: "
                + ", ".join(sorted(set(collisions))))
        target.mkdir(parents=True, exist_ok=True)
        for rel, path in planned.items():
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
        return sorted(str(rel) for rel in planned)

    def cleanup(self) -> None:
        """Terminate every worker's sandbox, best-effort each."""
        for worker in self._workers.values():
            try:
                worker["backend"].cleanup()
            except Exception:
                pass
        self._workers.clear()
