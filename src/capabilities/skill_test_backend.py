"""Run skill checks through the configured disposable execution backend."""

from pathlib import Path

from .execution import backend_is_sandboxed


class SkillTestBackend:
    def __init__(self, backend, workdir, timeout_seconds=120):
        if not backend_is_sandboxed(backend) or getattr(backend, "allow_network", True):
            raise ValueError("skill tests require an isolated, network-disabled execution backend")
        self.backend = backend
        self.workdir = Path(workdir)
        self.timeout_seconds = timeout_seconds

    def run_command(self, command):
        result = self.backend.run(["sh", "-c", command], self.workdir,
                                  timeout_seconds=self.timeout_seconds)
        if not getattr(result, "isolated", False):
            raise ValueError("skill test backend did not provide isolation")
        return result
