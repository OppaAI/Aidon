"""Run declared plugin acceptance checks against immutable candidate bytes."""
from pathlib import Path
import ast
import json
import shutil
import tempfile
import time
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


def _python_asserted_functions(code: str) -> set[str]:
    """Return functions actually called inside comparison assertions."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    checked = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert) or not isinstance(node.test, ast.Compare):
            continue
        for part in ast.walk(node.test):
            if isinstance(part, ast.Call):
                if isinstance(part.func, ast.Name):
                    checked.add(part.func.id)
                elif isinstance(part.func, ast.Attribute):
                    checked.add(part.func.attr)
    return checked


def _command_python_source(command: list[str], source: Path) -> str | None:
    executable = Path(command[0]).name.lower()
    if executable not in {"python", "python3", "pypy", "pypy3"}:
        return None
    tail = command[1:]
    if "-c" in tail:
        index = tail.index("-c")
        return tail[index + 1] if index + 1 < len(tail) else ""
    script = next((part for part in tail if part.endswith(".py")), None)
    if not script:
        return None
    path = (source / script).resolve()
    try:
        path.relative_to(source.resolve())
        return path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


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


def _mutation_copy(destination: Path, tool: dict) -> None:
    """Replace one declared tool body with a deterministic failure.

    Acceptance evidence is useful only if its checks notice when the behavior
    under review is broken. This bounded mutation check does not prove test
    completeness, but it rejects checks unrelated to a declared tool.
    """
    import ast

    module = (destination / str(tool.get("module", ""))).resolve()
    try:
        module.relative_to(destination.resolve())
        source_text = module.read_text(encoding="utf-8")
        tree = ast.parse(source_text)
    except (OSError, SyntaxError, ValueError) as exc:
        raise ValueError("cannot mutation-check declared tool module") from exc
    function_name = str(tool.get("function", ""))
    matches = [node for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == function_name]
    if len(matches) != 1:
        raise ValueError("cannot uniquely mutation-check declared tool function")
    function = matches[0]
    function.body = [ast.Raise(
        exc=ast.Call(func=ast.Name(id="RuntimeError", ctx=ast.Load()),
                     args=[ast.Constant("acceptance mutation")], keywords=[]),
        cause=None)]
    ast.fix_missing_locations(tree)
    module.write_text(ast.unparse(tree) + "\n", encoding="utf-8")


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
    tests compare results from every declared tool, and a mutation run checks
    that each tool's implementation is exercised. This still cannot prove
    completeness or that an external operation is safe.
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
    declared_tools = manifest.get("tools") or []
    if (not isinstance(declared_tools, list) or len(declared_tools) > 20):
        raise ValueError("plugin tools must be a list for mutation validation")
    required_functions = {str(tool.get("function", ""))
                          for tool in declared_tools if isinstance(tool, dict)}
    asserted_functions = set()
    for command in commands:
        code = _command_python_source(command, source)
        if code is not None:
            asserted_functions.update(_python_asserted_functions(code))
    missing_coverage = sorted(required_functions - asserted_functions)
    if missing_coverage:
        raise ValueError(
            "acceptance tests must assert the behavior of every declared tool; "
            "missing: " + ", ".join(missing_coverage))
    if getattr(backend, "name", "") not in ("modal", "docker", "bubblewrap"):
        raise ValueError("acceptance tests require an isolated Bubblewrap, Modal, or Docker backend")
    if getattr(backend, "allow_network", True):
        raise ValueError("acceptance tests require a network-disabled backend")
    original_hash = plugin_tree_hash(source)
    results = []
    deadline = time.monotonic() + 300

    def run_bounded(command, workspace):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("acceptance validation exceeded its 300-second budget")
        return backend.run(command, workspace,
                           timeout_seconds=min(120, max(1, int(remaining))))

    with tempfile.TemporaryDirectory(prefix="aidon-acceptance-") as tmp:
        workspace = Path(tmp) / "candidate"
        shutil.copytree(source, workspace)
        if plugin_tree_hash(workspace) != original_hash:
            raise ValueError("candidate changed while preparing tests")
        for command in commands:
            result = run_bounded(command, workspace)
            if not result.isolated or not result.ok:
                raise ValueError("acceptance test failed or did not run in isolation")
            results.append({"command": command, "returncode": result.returncode})
            if plugin_tree_hash(workspace) != original_hash:
                raise ValueError("acceptance test modified the candidate")
        mutation_evidence = []
        mutation_tools = [] if getattr(backend, "test_double", False) else declared_tools
        for tool in mutation_tools:
            if not isinstance(tool, dict):
                raise ValueError("invalid tool in mutation validation")
            with tempfile.TemporaryDirectory(prefix="aidon-mutation-") as mutated_tmp:
                mutated_workspace = Path(mutated_tmp) / "candidate"
                shutil.copytree(source, mutated_workspace)
                _mutation_copy(mutated_workspace, tool)
                mutation_killed = False
                for command in commands:
                    result = run_bounded(command, mutated_workspace)
                    if not result.isolated:
                        raise ValueError("mutation check did not run in isolation")
                    if not result.ok:
                        mutation_killed = True
                        break
                if not mutation_killed:
                    raise ValueError(
                        "acceptance tests did not detect broken behavior for "
                        f"tool {tool.get('name', tool.get('function', 'unknown'))!r}")
                mutation_evidence.append({
                    "tool": str(tool.get("name", "")), "detected": True})
    if plugin_tree_hash(source) != original_hash:
        raise ValueError("candidate changed during acceptance testing")
    return {"source_hash": original_hash, "backend": backend.name,
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "tests": results, "mutation_checks": mutation_evidence}
