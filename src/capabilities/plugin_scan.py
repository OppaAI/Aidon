"""Static plugin inspection, manifest validation, and loading."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path


MAX_PLUGIN_BYTES = 10 * 1024 * 1024

BLOCKING_TYPES = ("eval_exec", "subprocess", "network", "secret",
                    "dynamic_import", "unpinned_dependency")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_RE = re.compile(r"(api[_-]?key|password|token|secret)", re.IGNORECASE)


def _module_path(directory: Path, module_name: str) -> Path:
    """Resolve a manifest module and keep it inside the plugin tree."""
    root = Path(directory).resolve()
    candidate = (root / str(module_name)).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(
            f"plugin module is outside the plugin directory: {module_name}")
    return candidate


def _validate_module_paths(directory: Path) -> None:
    directory = Path(directory)
    manifest_path = directory / "plugin.json"
    if not manifest_path.is_file():
        return
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("plugin manifest must be a JSON object")
    tools = manifest.get("tools", []) or []
    if not isinstance(tools, list):
        raise ValueError("plugin tools must be a list")
    for tool in tools:
        if not isinstance(tool, dict):
            raise ValueError("plugin tool entries must be JSON objects")
        module_name = str(tool.get("module", ""))
        if module_name:
            _module_path(directory, module_name)


@dataclass
class ScanFinding:
    path: str
    line: int
    type: str
    message: str

    def to_dict(self) -> dict:
        return {"path": self.path, "line": self.line,
                "type": self.type, "message": self.message}


@dataclass
class Plugin:
    name: str
    version: str
    description: str = ""
    directory: Path | None = None
    tools: list[dict] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    functions: dict = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)

    def tool_names(self) -> list[str]:
        return [str(t.get("name", "")) for t in self.tools if t.get("name")]

    def tool_spec(self, name: str) -> dict | None:
        for tool in self.tools:
            if str(tool.get("name", "")) == str(name):
                return tool
        return None

    def needs_context(self, name: str) -> bool:
        spec = self.tool_spec(name)
        return bool(spec and spec.get("context"))


def _capability_policy(capabilities):
    """Parse and validate capabilities, importing lazily so the
    runtime module stays optional for callers that never use it."""
    from .plugin_runtime import CapabilityPolicy
    return CapabilityPolicy.from_capabilities(capabilities)


def plugin_is_adopted(plugin) -> bool:
    """True only for a plugin the owner approved.

    ``commit_plugin`` writes a ``.adopted`` marker into the installed
    copy after approval. A directory hand-placed in ``<home>/toolbox/``
    has no marker, so it never gets capabilities and never runs a
    sandbox tool: no approval, no power.
    """
    directory = getattr(plugin, "directory", None)
    if directory is None:
        return False
    directory = Path(directory)
    try:
        marker = json.loads((directory / ".adopted").read_text(
            encoding="utf-8"))
        if not isinstance(marker, dict):
            return False
        approved_hash = str(marker.get("approved_hash", ""))
        return bool(approved_hash) and plugin_tree_hash(directory) == approved_hash
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False


def _existing_approved_plugin(registry, name: str, approved_hash: str):
    """Return the already-installed exact approved artifact, if intact."""
    target = Path(registry.toolbox_dir) / str(name)
    try:
        static = load_plugin(target, execute=False)
        if static.name != name or not plugin_is_adopted(static):
            return None
        marker = json.loads((target / ".adopted").read_text(encoding="utf-8"))
        evidence = marker.get("acceptance_evidence") or {}
        if (marker.get("approved_hash") != approved_hash
                or evidence.get("source_hash") != approved_hash
                or not evidence.get("tests")
                or any(item.get("returncode") != 0
                       for item in evidence["tests"])):
            return None
        return load_plugin(target)
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def adoption_gate(posture: str, kernel_mode: bool,
                  capabilities, sandboxed: bool = False) -> tuple[bool, str]:
    """Decide whether the current tool posture allows an adoption.

    Returns ``(allowed, reason)``. ``locked`` (and kernel mode, which
    is the same posture by another name) refuses every adoption.
    ``standard`` refuses capability and sandbox plugins but allows
    pure tools. ``developer`` and ``selfmod`` allow both; the owner's
    approval card still lists every capability, so the posture widens
    what can be *asked for*, never what is granted without a decision.
    """
    posture = str(posture or "standard").strip().lower()
    if kernel_mode or posture == "locked":
        return False, "tool posture is locked; plugin adoption is disabled"
    if posture == "standard" and (list(capabilities or ()) or sandboxed):
        return False, ("posture 'standard' allows pure tools only; a "
                       "capability or sandbox plugin needs "
                       "AIDON_TOOL_POSTURE=developer or selfmod")
    if posture == "standard":
        return True, ""
    if posture in ("developer", "selfmod"):
        return True, ""
    return False, f"unknown tool posture: {posture}"


def sandbox_module_paths(directory) -> set[str]:
    """Relative module paths that are tool code for sandbox-mode tools.

    A module is treated as sandbox code only when every tool that
    names it is ``"mode": "sandbox"``. A module shared with an inline
    tool stays fully scanned, because that inline tool would import it
    into Aidon's process.
    """
    from pathlib import Path
    manifest_path = Path(directory) / "plugin.json"
    if not manifest_path.is_file():
        return set()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    sandboxed: set[str] = set()
    inline: set[str] = set()
    for tool in manifest.get("tools", []) or []:
        if not isinstance(tool, dict):
            continue
        module = str(tool.get("module", ""))
        if not module:
            continue
        if str(tool.get("mode", "")).lower() == "sandbox":
            sandboxed.add(module)
        else:
            inline.add(module)
    return sandboxed - inline


def scan_plugin(directory: Path) -> list[ScanFinding]:
    """Security scan, path/line/type only, never values."""
    findings: list[ScanFinding] = []
    directory = Path(directory)
    _validate_module_paths(directory)
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(directory))
        if path.name in ("requirements.txt", "requirements-dev.txt"):
            for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.endswith(("*", "latest")) or "==" not in stripped:
                    findings.append(ScanFinding(rel, i, "unpinned_dependency",
                                                "Dependency is *, latest, or unpinned"))
            continue
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            findings.append(ScanFinding(rel, 0, "unparsable", "Python file does not parse"))
            continue
        subprocess_names: set[str] = set()
        for _node in ast.walk(tree):
            if isinstance(_node, ast.ImportFrom) and _node.module == "subprocess":
                for _alias in _node.names:
                    subprocess_names.add(_alias.asname or _alias.name)
            elif isinstance(_node, ast.Import):
                for _alias in _node.names:
                    if _alias.name == "subprocess":
                        subprocess_names.add(_alias.asname or "subprocess")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name == "__import__" or name == "import_module":
                    findings.append(ScanFinding(rel, node.lineno, "dynamic_import",
                                                "Dynamic import hides the imported module"))
                elif name in ("eval", "exec"):
                    findings.append(ScanFinding(rel, node.lineno, "eval_exec",
                                                "eval or exec call"))
                elif name in ("system", "popen") or (isinstance(func, ast.Attribute) and name in ("Popen", "run") and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    findings.append(ScanFinding(rel, node.lineno, "subprocess",
                                                "Subprocess or shell call"))
                elif isinstance(func, ast.Name) and func.id in subprocess_names:
                    findings.append(ScanFinding(rel, node.lineno, "subprocess",
                                                "Subprocess call via imported name"))
                elif isinstance(func, ast.Attribute) and func.attr in ("execv", "execl", "execle", "execlp", "execvp", "execvpe", "spawnl", "spawnlp", "spawnv", "spawnvp", "spawnve", "spawnlpe"):
                    findings.append(ScanFinding(rel, node.lineno, "subprocess",
                                                "Process spawn/exec call"))
                elif name == "urlopen":
                    findings.append(ScanFinding(rel, node.lineno, "network",
                                                "Network call"))
                elif name in ("get", "post", "request") and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in ("requests", "urllib", "http"):
                    # Only a network client's get/post, never a
                    # dict's .get(), which is ordinary plugin code.
                    findings.append(ScanFinding(rel, node.lineno, "network",
                                                "Network call"))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("socket", "subprocess"):
                        findings.append(ScanFinding(rel, node.lineno, "network" if alias.name == "socket" else "subprocess",
                                                    f"Import of {alias.name}"))
                    elif alias.name == "importlib" or alias.name.startswith("importlib."):
                        findings.append(ScanFinding(rel, node.lineno, "dynamic_import",
                                                    "Dynamic import hides the imported module"))
            if isinstance(node, ast.ImportFrom) and (node.module in ("socket", "subprocess", "urllib.request") or (node.module or "").startswith("importlib")):
                if (node.module or "").startswith("importlib"):
                    findings.append(ScanFinding(rel, node.lineno, "dynamic_import",
                                                "Dynamic import hides the imported module"))
                else:
                    findings.append(ScanFinding(rel, node.lineno, "network" if node.module != "subprocess" else "subprocess",
                                                f"Import from {node.module}"))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = node.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    for target in targets:
                        if isinstance(target, ast.Name) and SECRET_RE.search(target.id):
                            findings.append(ScanFinding(rel, node.lineno, "secret",
                                                        "Secret assigned to a string value"))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                # A write open in plugin code is a warning, not
                # a block: pure tools should not write files,
                # but the owner can approve a plugin that does.
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and "w" in str(node.args[1].value):
                    findings.append(ScanFinding(rel, node.lineno, "file_write",
                                                "File write in plugin code"))
    # Sandbox-mode modules run isolated, so the execution-side patterns
    # (subprocess, network, eval/exec, dynamic import) are expected
    # there. Downgrade them to non-blocking names so the card reports
    # them without refusing adoption. Secret literals still block: a
    # key baked into plugin source leaks no matter where it runs.
    sandboxed = sandbox_module_paths(directory)
    if sandboxed:
        for finding in findings:
            if (str(finding.path).replace("\\", "/") in sandboxed
                    and finding.type in ("subprocess", "network",
                                         "eval_exec", "dynamic_import")):
                finding.type = "sandbox_" + finding.type
                finding.message = "sandboxed tool code: " + finding.message
    return findings


def load_plugin(directory: Path, execute: bool = True) -> Plugin:
    """Load and validate a plugin directory.

    ``execute=False`` validates the manifest and that each named
    function is defined at module top level, using the AST only. That
    is the pre-approval path: importing a module runs its top-level
    code, so importing before the owner approves would execute
    unreviewed code on the host. After approval, ``execute=True``
    imports it and binds the real callables.
    """
    directory = Path(directory)
    _validate_module_paths(directory)
    manifest_path = directory / "plugin.json"
    if not manifest_path.exists():
        raise ValueError("plugin.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    name = str(manifest.get("name", ""))
    if not NAME_RE.match(name):
        raise ValueError(f"invalid plugin name: {name}")
    version = str(manifest.get("version", ""))
    if not version:
        raise ValueError("plugin version is missing")
    plugin = Plugin(name=name, version=version,
                    description=str(manifest.get("description", "")),
                    directory=directory,
                    tools=list(manifest.get("tools", [])),
                    skills=list(manifest.get("skills", [])),
                    capabilities=[str(c) for c in
                                  manifest.get("capabilities", []) or []])
    # Validate capabilities now, before any approval card: an unknown
    # capability is a hard error, never a silent no-op.
    _capability_policy(plugin.capabilities)
    for tool in plugin.tools:
        module_name = str(tool.get("module", ""))
        func_name = str(tool.get("function", ""))
        tool_name = str(tool.get("name", ""))
        if "accepts_attachments" in tool and not isinstance(
                tool.get("accepts_attachments"), bool):
            raise ValueError("accepts_attachments must be true or false")
        if not module_name or not func_name or not tool_name:
            raise ValueError("plugin tool needs name, module, and function")
        module_path = _module_path(directory, module_name)
        if not module_path.exists():
            raise ValueError(f"plugin module missing: {module_name}")
        sandboxed = str(tool.get("mode", "")).lower() == "sandbox"
        if tool.get("accepts_attachments") and not sandboxed:
            raise ValueError(
                "accepts_attachments is only allowed for sandbox tools")
        if not execute or sandboxed:
            # Static check only: no import, no top-level code runs.
            # Sandbox tools keep this path even after approval: their
            # module must never be imported into Aidon's process, only
            # into the isolated runner.
            try:
                tree = ast.parse(module_path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError) as exc:
                raise ValueError(
                    f"plugin module does not parse: {module_name}: {exc}")
            defined = {node.name for node in tree.body
                       if isinstance(node, (ast.FunctionDef,
                                            ast.AsyncFunctionDef,
                                            ast.ClassDef))}
            if func_name not in defined:
                raise ValueError(
                    f"plugin function is not defined: {func_name}")
            continue
        # Python bytecode is deliberately excluded from the adoption hash.
        # Remove any supplied/generated cache before an adopted source file
        # is imported so ignored bytecode can never replace approved source.
        for cache_dir in directory.rglob("__pycache__"):
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir, ignore_errors=True)
        spec = importlib.util.spec_from_file_location(
            f"aidon_plugin_{name}_{tool_name}", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        func = getattr(module, func_name, None)
        if not callable(func):
            raise ValueError(f"plugin function is not callable: {func_name}")
        plugin.functions[tool_name] = func
    for skill in plugin.skills:
        if not (directory / str(skill)).exists():
            raise ValueError(f"plugin skill missing: {skill}")
    return plugin




def plugin_tree_hash(source_dir: Path) -> str:
    """Hash adopted source files, excluding approval and bytecode artifacts."""
    source_dir = Path(source_dir)
    digest = hashlib.sha256()
    for path in sorted(source_dir.rglob("*"),
                       key=lambda p: str(p.relative_to(source_dir))):
        rel = str(path.relative_to(source_dir))
        if (rel == ".adopted" or "__pycache__" in path.parts
                or path.suffix == ".pyc"):
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        if path.is_file():
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()
