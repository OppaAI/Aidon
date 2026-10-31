"""Run declared plugin acceptance checks against immutable candidate bytes."""
from pathlib import Path
import json
import shutil
import tempfile
from datetime import datetime, timezone

from .plugins import plugin_tree_hash


def acceptance_backend(config, workspace):
    """Create a fresh, network-disabled backend for one acceptance run."""
    from .execution import make_backend
    from .sandbox import Sandbox
    return make_backend(config.execution_backend, Sandbox(workspace, require_isolation=True),
                        image=config.docker_image, allow_network=False,
                        modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                        modal_memory_mb=config.modal_memory_mb)


def test_candidate(source, backend):
    """Return evidence or raise; callers must bind approval to ``source_hash``.

    Commands are argv arrays executed in a disposable copy, with no host shell.
    Evidence proves the listed commands exited successfully, not that generated
    tests are comprehensive or that an external operation is safe.
    """
    source = Path(source)
    manifest = json.loads((source / "plugin.json").read_text(encoding="utf-8"))
    commands = manifest.get("acceptance_tests")
    if (not isinstance(commands, list) or not commands or len(commands) > 10
            or any(not isinstance(c, list) or not c or len(c) > 32
                   or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in c)
                   for c in commands)):
        raise ValueError("acceptance_tests must contain 1–10 nonempty argv arrays")
    if getattr(backend, "name", "") not in ("modal", "docker", "bubblewrap"):
        raise ValueError("acceptance tests require an isolated Bubblewrap, Modal, or Docker backend")
    if getattr(backend, "allow_network", True):
        raise ValueError("acceptance tests require a network-disabled backend")
    original_hash = plugin_tree_hash(source)
    results = []
    with tempfile.TemporaryDirectory(prefix="aidon-acceptance-") as tmp:
        workspace = Path(tmp) / "candidate"
        shutil.copytree(source, workspace)
        if plugin_tree_hash(workspace) != original_hash:
            raise ValueError("candidate changed while preparing tests")
        for command in commands:
            result = backend.run(command, workspace, timeout_seconds=120)
            if not result.isolated or not result.ok:
                raise ValueError("acceptance test failed or did not run in isolation")
            results.append({"command": command, "returncode": result.returncode})
            if plugin_tree_hash(workspace) != original_hash:
                raise ValueError("acceptance test modified the candidate")
    if plugin_tree_hash(source) != original_hash:
        raise ValueError("candidate changed during acceptance testing")
    return {"source_hash": original_hash, "backend": backend.name,
            "tested_at": datetime.now(timezone.utc).isoformat(), "tests": results}
