"""Run declared plugin acceptance checks against immutable candidate bytes."""
from pathlib import Path
import ast
import json
import shutil
import tempfile
from datetime import datetime, timezone

from .plugins import plugin_tree_hash


def _python_check_is_dynamic(node) -> bool:
    """Reject assertions made only from constants/operators."""
    return any(isinstance(part, (ast.Name, ast.Call, ast.Attribute,
                                 ast.Subscript, ast.Await, ast.Yield))
               for part in ast.walk(node))


def _python_code_has_check(code: str) -> bool:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True  # let the isolated runner report syntax failures
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert) and _python_check_is_dynamic(node.test):
            return True
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and (func.attr.startswith("assert") or func.attr in {
                        "fail", "failUnless", "raises"})):
                return True
    return False


def _reject_obvious_noop(command: list[str], source: Path) -> None:
    """Refuse known commands that report success without checking behavior.

    This is a narrow guard against vacuous evidence, not a proof that a test
    meaningfully covers its plugin. The owner still reviews the test source.
    """
    executable = Path(command[0]).name.lower()
    tail = [str(part).strip() for part in command[1:]]
    if (executable in {"true", ":"}
            or (executable in {"sh", "bash", "dash"}
                and len(tail) >= 2 and tail[0] == "-c"
                and tail[1].strip() in {"true", ":", "exit 0"})
            or (executable in {"echo", "printf"})
            or (executable in {"python", "python3", "pypy", "pypy3"}
                and any(arg in {"--version", "-V", "--help", "-h"}
                        for arg in tail))):
        raise ValueError("acceptance test is an obvious no-op; add a behavioral assertion")
    if executable not in {"python", "python3", "pypy", "pypy3"}:
        return
    if "-c" in tail:
        code = tail[tail.index("-c") + 1] if tail.index("-c") + 1 < len(tail) else ""
        if not _python_code_has_check(code):
            raise ValueError("Python acceptance command has no behavioral assertion")
        return
    script = next((part for part in tail if part.endswith(".py")), None)
    if script:
        candidate = (source / script).resolve()
        try:
            candidate.relative_to(source.resolve())
            code = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise ValueError(
                "Python acceptance script is missing or outside the candidate") from exc
        if not _python_code_has_check(code):
            raise ValueError("Python acceptance script has no behavioral assertion")


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
    for command in commands:
        _reject_obvious_noop(command, source)
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
