"""Explicit isolated-backend stand-in for plugin adoption unit tests."""
from types import SimpleNamespace


def passing_backend_factory():
    """Model successful isolated runner startup without invoking the host."""
    class Backend:
        name = "docker"
        allow_network = False

        def run(self, command, workspace, timeout_seconds):
            return SimpleNamespace(isolated=True, ok=True, returncode=0)

        def cleanup(self):
            pass

    return Backend()
