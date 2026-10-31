"""Tool, skill, and plugin system, arena side.

Aidon builds his own capability tools by climbing:
research, draft a plugin, sandbox-test it, and adopt it
with the owner's approval. Megumi builds the arena
around that climb, never the capability tools themselves.
This module is that arena: a plugin format, a loader,
a registry, a security scan, and an adoption gate.

Definitions, kept separate:
- A tool is code: a Python function in a plugin directory,
  called with a dict of args, returning text. Tools are
  pure by design: they take args and return text, they
  do not get the sandbox, the home, or a network client.
  File and network work stays with the sandbox tools
  and with later, explicitly approved capability plugins.
- A skill is instructions: a Markdown file in a plugin
  directory, read by the planner as context, never
  executed as code. A skill tells the model how to do
  a job, a tool does a step of it.
- A plugin is a bundle: a directory with a plugin.json
  manifest (name, version, description, tools, skills),
  plus the tool modules and skill files it names.
  Plugins live in <home>/toolbox/<name>/, never in the
  repo, never in a central service.

Adoption, in order, no step skipped:
1. Scan the plugin directory. The scan reports path,
   line, and type only, never values, never a secret
   itself, and never an invented CVE id.
2. Blocking findings reject adoption, even if the user
   would approve: eval, exec, subprocess or os.system,
   socket or network calls, a secret assignment
   (a key, token, or password set to a string value),
   a dynamic import (which hides the imported module
   from this scan), and a dependency that is *, latest,
   or unpinned (an unpinned build is not reproducible,
   so it cannot be trusted).
3. Non-blocking warnings are shown on the adoption card:
   a file write in plugin code. Warnings do not block,
   they are disclosed, and adoption still needs approval.
4. The owner approves on a decision card, with the scan
   report as the card body. No approval, no adoption.
5. Only then is the plugin copied to <home>/toolbox/,
   and recorded as episodic memory and in learned/.

Manifest validation, by code:
name is letters, digits, dash, or underscore only,
version is present, every tool names a module file
that exists in the plugin directory and a function
that is callable after loading that module, every
skill names a Markdown file that exists. A plugin
that fails validation is not adopted, and is not
loaded into the registry.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
import hashlib

# Adoption size cap: a plugin tree larger than this is rejected before
# any approval card, so an oversized or bloated source never reaches
# the owner as something to approve.
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
    """Deterministic sha256 over adopted plugin source files.

    The marker and Python cache files are excluded so the same approved
    hash can be checked after installation and interpreter imports.
    """
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


def plugin_tree_size(source_dir: Path) -> int:
    """Total bytes of all files in a plugin source tree."""
    source_dir = Path(source_dir)
    return sum(p.stat().st_size for p in source_dir.rglob("*") if p.is_file())



class Preflight:
    """Everything adoption needs except the owner's decision
    and the copy itself: the validated plugin, the scan and
    security-gate findings, the approved tree hash, and the
    approval card body. Built without approval and without
    touching the installed plugins directory, so a caller
    can approve first and only then spend budget."""

    def __init__(self, ok, plugin=None, findings=None,
                 gate_findings=None, approved_hash="", body="",
                 message=""):
        self.ok = ok
        self.plugin = plugin
        self.findings = findings or []
        self.gate_findings = gate_findings or []
        self.approved_hash = approved_hash
        self.body = body
        self.message = message


def preflight_plugin(source_dir: Path) -> Preflight:
    """Scan, size-cap, hash, security-gate, and statically
    validate a plugin source tree. No approval, no copy,
    no import of the plugin's code (validation is AST-only).
    A failed preflight is a refusal, never a budget charge."""
    source_dir = Path(source_dir)
    try:
        findings = scan_plugin(source_dir)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return Preflight(False, message=(
            f"Adoption rejected: invalid plugin paths or manifest: "
            f"{type(exc).__name__}"))
    blocking = [f for f in findings if f.type in BLOCKING_TYPES]
    if blocking:
        return Preflight(False, findings=findings, message=(
            "Adoption rejected: blocking scan findings ("
            + ", ".join(sorted({f.type for f in blocking})) + ")"))
    # Size cap, before any approval card: an oversized source is
    # not something the owner should be asked to approve.
    total_size = plugin_tree_size(source_dir)
    if total_size > MAX_PLUGIN_BYTES:
        return Preflight(False, findings=findings, message=(
            f"Adoption rejected: plugin source is {total_size} bytes, "
            f"over the {MAX_PLUGIN_BYTES} byte cap"))
    # Approval binding: hash the approved bytes now, re-hash
    # immediately before the copy, and abort if they changed.
    try:
        approved_hash = plugin_tree_hash(source_dir)
    except OSError as exc:
        return Preflight(False, findings=findings, message=(
            f"Adoption rejected: cannot hash source: {exc}"))
    # Full security gate over the plugin's own code: a fail
    # verdict (os.system, eval/exec, piped download-to-shell,
    # secret-like strings) blocks adoption even when the
    # pattern scan above passed; review verdicts go on the
    # approval card below.
    gate_findings = []
    sandboxed = sandbox_module_paths(source_dir)
    try:
        from .security_gate import gate_text
        for py_path in sorted(source_dir.rglob("*.py")):
            rel = str(py_path.relative_to(source_dir)).replace("\\", "/")
            try:
                result = gate_text(str(py_path), py_path.read_text(encoding="utf-8"))
            except OSError:
                continue
            gate_findings.extend(result.findings)
            if result.verdict == "fail":
                if rel in sandboxed:
                    # Permit only execution patterns that the bubblewrap
                    # boundary contains. Other blocks (including secrets,
                    # destructive commands, and install scripts) still fail.
                    allowed_execution_blocks = {
                        "os.system() call",
                        "eval()/exec() dynamic code execution",
                    }
                    blocking_findings = [
                        finding for finding in result.findings
                        if finding.severity == "block"]
                    if (blocking_findings and all(
                            finding.source == "builtin"
                            and finding.message in allowed_execution_blocks
                            for finding in blocking_findings)):
                        continue
                return Preflight(False, findings=findings,
                                 gate_findings=gate_findings,
                                 message="Adoption rejected: security gate fail")
    except Exception as exc:
        # Fail closed: a gate that failed to run must never present
        # as a clean scan. The old code reset findings to [] and
        # proceeded to an approval card showing "no findings".
        return Preflight(False, findings=findings,
                         message=f"Adoption rejected: security gate error: {exc}")
    try:
        # Static validation only. Importing the module here would
        # run its top-level code on the host before the owner has
        # seen the scan report or approved anything.
        plugin = load_plugin(source_dir, execute=False)
    except Exception as exc:
        return Preflight(False, findings=findings,
                         gate_findings=gate_findings,
                         message=f"Adoption rejected: invalid plugin: {exc}")
    # Approval card: scan report as body, path/line/type only.
    body_lines = [f"Plugin: {plugin.name} {plugin.version}",
                  f"Description: {plugin.description}",
                  f"Tools: {', '.join(plugin.tool_names()) or 'none'}",
                  f"Skills: {', '.join(plugin.skills) or 'none'}"]
    policy = _capability_policy(plugin.capabilities)
    body_lines.append(f"Capabilities: {policy.describe()}")
    sandbox_tools = [str(t.get("name", "")) for t in plugin.tools
                     if str(t.get("mode", "")).lower() == "sandbox"]
    if sandbox_tools:
        body_lines.append(
            "Sandbox tools (run isolated; subprocess/network/eval allowed "
            f"inside): {', '.join(sandbox_tools)}")
    attachment_tools = [str(t.get("name", "")) for t in plugin.tools
                        if str(t.get("mode", "")).lower() == "sandbox"
                        and t.get("accepts_attachments")]
    if attachment_tools:
        body_lines.append(
            "These tools receive copies of the current goal's attached "
            "images inside their isolated run directory: "
            + ", ".join(attachment_tools))
    body_lines.append(f"Approved source hash: {approved_hash}")
    if findings:
        body_lines.append("Scan warnings:")
        body_lines.extend(f"  {f.path}:{f.line} {f.type}" for f in findings)
    else:
        body_lines.append("Scan: no findings")
    if gate_findings:
        body_lines.append("Security gate findings:")
        body_lines.extend(f"  {f.severity} {f.source}: {f.message}" for f in gate_findings)
    return Preflight(True, plugin=plugin, findings=findings,
                     gate_findings=gate_findings,
                     approved_hash=approved_hash,
                     body="\n".join(body_lines))


def ask_plugin_approval(gate, approve, preflight: Preflight) -> bool:
    """Ask the owner about a preflighted plugin. Adoption is an
    explicit install decision, so graduated autonomy and preapproval
    cannot approve it implicitly."""
    approved = False
    if approve is not None:
        # Callback approvers see exactly what the gate.ask path
        # shows: the approved tree hash and the card body, so
        # the approval binds to the same bytes. Older
        # two-argument callbacks keep working via fallback.
        try:
            approved = bool(approve(preflight.plugin, preflight.findings,
                                    preflight.approved_hash, preflight.body))
        except TypeError:
            approved = bool(approve(preflight.plugin, preflight.findings))
    elif gate is not None:
        # Gates from out-of-tree callers and test doubles may take
        # only (kind, title, body). The action_class is an
        # autonomy hint, not part of the decision, so a plain gate
        # is asked without it rather than crashing adoption.
        try:
            decision = gate.ask(
                "plugin",
                "Adopt this plugin?",
                preflight.body,
                action_class="plugin",
                force_ask=True,
            )
        except TypeError:
            try:
                decision = gate.ask("plugin", "Adopt this plugin?",
                                    preflight.body, force_ask=True)
            except TypeError:
                decision = gate.ask("plugin", "Adopt this plugin?",
                                    preflight.body)
        approved = decision.verdict == "approved"
    return approved


def _commit_plugin_locked(registry, source_dir: Path, preflight: Preflight,
                  memory=None, conversation_id: str = "", test_evidence=None) -> tuple[bool, list, str]:
    """Copy a preflighted, approved plugin into place. The
    re-hash immediately before the copy is the binding: if
    the source changed after approval, the approval no
    longer covers these bytes, so abort and leave any
    previously installed version untouched."""
    source_dir = Path(source_dir)
    plugin = preflight.plugin
    findings = preflight.findings
    try:
        current_hash = plugin_tree_hash(source_dir)
    except OSError as exc:
        return False, findings, f"Adoption aborted: cannot re-hash source: {exc}"
    if current_hash != preflight.approved_hash:
        return False, findings, (
            "Adoption aborted: plugin source changed after approval "
            f"(approved hash {preflight.approved_hash}, current hash {current_hash}); "
            "approve the new source to adopt it")
    if (not isinstance(test_evidence, dict)
            or test_evidence.get("backend") not in ("modal", "docker")
            or not isinstance(test_evidence.get("tests"), list)
            or not test_evidence["tests"]
            or any(not isinstance(result, dict)
                   or result.get("returncode") != 0
                   for result in test_evidence["tests"])):
        return False, findings, (
            "Adoption refused: verified isolated acceptance-test evidence is required")
    if test_evidence.get("source_hash") != current_hash:
        return False, findings, "Adoption aborted: test evidence does not match candidate"
    target = registry.toolbox_dir / plugin.name
    registry.toolbox_dir.mkdir(parents=True, exist_ok=True)
    import tempfile
    import uuid
    stage = Path(tempfile.mkdtemp(prefix=".adoption-", dir=registry.toolbox_dir))
    backup = registry.toolbox_dir / (".backup-" + uuid.uuid4().hex)
    replaced = False
    installed = False
    try:
        shutil.copytree(source_dir, stage, dirs_exist_ok=True)
        if plugin_tree_hash(stage) != preflight.approved_hash:
            return False, findings, "Adoption aborted: staged plugin differs from approved source"
        marker = {"approved_hash": preflight.approved_hash}
        if test_evidence is not None:
            marker["acceptance_evidence"] = test_evidence
        (stage / ".adopted").write_text(json.dumps(marker), encoding="utf-8")
        if target.exists():
            target.rename(backup)
            replaced = True
        stage.rename(target)
        installed = True
        loaded = load_plugin(target)
        registry.plugins[plugin.name] = loaded
    except Exception as exc:
        if installed:
            shutil.rmtree(target, ignore_errors=True)
        if replaced:
            backup.rename(target)
        return False, findings, f"Adoption aborted; previous version preserved: {type(exc).__name__}"
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    # Replacement invalidates cached health: the old verdict
    # judged the previous bytes, not these.
    from .capability_hub import invalidate_health
    invalidate_health(registry.home, f"plugin:{plugin.name}")
    for skill in plugin.skills:
        invalidate_health(registry.home,
                          f"skill:{plugin.name}/{skill}")
    if memory is not None:
        try:
            memory.record_episodic(
                conversation_id, f"Adopted plugin {plugin.name}",
                {"plugin": plugin.name, "version": plugin.version,
                 "tools": plugin.tool_names()})
        except Exception:
            pass
    return True, findings, f"Adopted plugin {plugin.name}"


def _toolbox_lock(registry):
    from contextlib import contextmanager
    import fcntl
    @contextmanager
    def locked():
        registry.toolbox_dir.mkdir(parents=True, exist_ok=True)
        with (registry.home / ".toolbox.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    return locked()


def commit_plugin(registry, source_dir: Path, preflight: Preflight,
                  memory=None, conversation_id: str = "", test_evidence=None):
    with _toolbox_lock(registry):
        return _commit_plugin_locked(registry, source_dir, preflight, memory, conversation_id, test_evidence)


class PluginRegistry:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.toolbox_dir = self.home / "toolbox"
        self.plugins: dict[str, Plugin] = {}
        self.skills_dir = self.home / "skills"

    def load_all(self) -> list[Plugin]:
        """Load every valid plugin in <home>/toolbox/, skip invalid ones."""
        with _toolbox_lock(self):
            return self._load_all_locked()

    def _load_all_locked(self):
        self.plugins = {}
        if not self.toolbox_dir.exists():
            return []
        # A crash after renaming the old plugin can leave only a backup.
        # Restore only an approved, intact backup; never overwrite a target.
        for backup in sorted(self.toolbox_dir.glob(".backup-*")):
            try:
                plugin = load_plugin(backup, execute=False)
                target = self.toolbox_dir / plugin.name
                if plugin_is_adopted(plugin) and not target.exists():
                    backup.rename(target)
                elif plugin_is_adopted(plugin) and target.is_dir():
                    # A crash after the new version was installed may leave
                    # the old backup behind. Only remove it after verifying
                    # the current target has an intact approval marker.
                    current = load_plugin(target, execute=False)
                    if current.name == plugin.name and plugin_is_adopted(current):
                        shutil.rmtree(backup)
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for path in sorted(self.toolbox_dir.iterdir()):
            if path.name.startswith(".") or not path.is_dir():
                continue
            try:
                # Unadopted directories are untrusted data. Validate their
                # approved bytes statically before importing any module code.
                if not (path / ".adopted").is_file():
                    continue
                static_plugin = load_plugin(path, execute=False)
                if not plugin_is_adopted(static_plugin):
                    continue
                plugin = load_plugin(path)
                self.plugins[plugin.name] = plugin
            except Exception:
                continue
        return list(self.plugins.values())

    def tool_names(self) -> list[str]:
        names: list[str] = []
        for plugin in list(self.plugins.values()):
            if not plugin_is_adopted(plugin):
                continue
            names.extend(plugin.tool_names())
        return sorted(names)

    def tool_owner(self, name: str):
        """Return the adopted plugin and manifest entry owning a tool."""
        for plugin in list(self.plugins.values()):
            if not plugin_is_adopted(plugin):
                continue
            spec = getattr(plugin, "tool_spec", lambda _n: None)(name)
            if spec is not None:
                return plugin, spec
        return None, None

    def skill_texts(self) -> dict[str, str]:
        """Skill texts for run context: plugin skills plus
        standalone skills the library trusts.

        Plugin skills are trusted by adoption (owner
        approval at adopt time). A standalone
        skills/*.md file enters run context only when a
        library skill with the same sanitized name is
        trusted: trust() publishes the Markdown, retire
        removes it, and hand-placed files without a
        trusted library record are unvetted drafts. They
        stay on disk and in the library list for
        development and approval, but never in a plan
        prompt, a skill expansion, or a recovery note.
        A library read failure fails closed: standalone
        files drop out, plugin skills are unaffected."""
        texts: dict[str, str] = {}
        for plugin in list(self.plugins.values()):
            if not plugin_is_adopted(plugin):
                continue
            for skill in plugin.skills:
                path = plugin.directory / str(skill)
                texts[f"{plugin.name}/{skill}"] = path.read_text(encoding="utf-8")
        # Standalone skills in <home>/skills/, Markdown only.
        if self.skills_dir.exists():
            try:
                from .skill_library import SkillLibrary
                texts.update(SkillLibrary(self.home).trusted_markdown())
            except Exception:
                pass
        return texts

    def _trusted_skill_stems(self) -> set[str]:
        """Sanitized stems of trusted library skills, matching
        the filename rule trust() publishes with."""
        try:
            from .skill_library import SkillLibrary
            return {Path(name).stem for name in
                    SkillLibrary(self.home).trusted_markdown()}
        except Exception:
            return set()

    def call(self, name: str, args: dict, ctx=None,
             context_factory=None, sandbox=None, attachments=None,
             approval_callback=None, execution_backend=None) -> str:
        """Dispatch a plugin tool.

        Three shapes, in order:
        - ``"mode": "sandbox"`` -> run the module in an isolated
          subprocess (Tier 2); ``sandbox`` must be provided.
        - ``"context": true`` -> ``fn(args, ctx)`` with the broker.
        - otherwise -> the original pure ``fn(args)``.

        A tool that needs a broker or a sandbox but gets neither fails
        loudly instead of silently dropping its capabilities.
        """
        for plugin in list(self.plugins.values()):
            if not plugin_is_adopted(plugin):
                continue
            spec = getattr(plugin, "tool_spec", lambda _n: None)(name)
            if spec is not None and str(spec.get("mode", "")).lower() == "sandbox":
                if not plugin_is_adopted(plugin):
                    raise ValueError(
                        f"plugin tool {name} is not adopted; a sandbox "
                        "tool runs only after owner approval")
                if sandbox is None:
                    raise ValueError(
                        f"plugin tool {name} is a sandbox tool but no "
                        "sandbox was provided")
                policy = _capability_policy(
                    getattr(plugin, "capabilities", []))
                invocation_approved = False
                if policy.network or policy.secrets:
                    if approval_callback is None:
                        raise PermissionError(
                            f"sandbox plugin {name} needs a fresh live "
                            "approval before network or secret access")
                    if not approval_callback(
                            plugin, spec, dict(args),
                            list(attachments or []) if spec.get(
                                "accepts_attachments") else []):
                        raise PermissionError(
                            f"sandbox plugin {name} invocation was rejected")
                    invocation_approved = True
                from .plugin_runtime import run_sandbox_tool
                return run_sandbox_tool(plugin, spec, dict(args),
                                        sandbox=sandbox, home=self.home,
                                        attachments=attachments,
                                        execution_backend=execution_backend,
                                        invocation_approved=invocation_approved)
            functions = getattr(plugin, "functions", {})
            if name not in functions:
                continue
            func = functions[name]
            needs_context = getattr(plugin, "needs_context", None)
            if callable(needs_context) and needs_context(name):
                broker = ctx
                if broker is None and context_factory is not None:
                    broker = context_factory(plugin)
                if broker is None:
                    raise ValueError(
                        f"plugin tool {name} requires a runtime context "
                        "but none was provided")
                return str(func(dict(args), broker))
            return str(func(dict(args)))
        raise ValueError(f"unknown plugin tool: {name}")

    def context_for(self, name: str, home: Path, sandbox=None,
                    network_available: bool | None = None):
        """Build the broker for the plugin that owns ``name``.

        Capabilities are granted only to an adopted plugin (one with
        the ``.adopted`` marker commit_plugin writes after approval).
        A hand-placed plugin still runs as a pure tool, but every
        broker method refuses, because no owner approved its powers.
        """
        from .plugin_runtime import build_context
        for plugin in list(self.plugins.values()):
            spec = getattr(plugin, "tool_spec", lambda _n: None)(name)
            if spec is None and name not in getattr(plugin, "functions", {}):
                continue
            adopted = plugin_is_adopted(plugin)
            capabilities = getattr(plugin, "capabilities", []) if adopted else []
            return build_context(getattr(plugin, "name", ""), home,
                                 capabilities, sandbox=sandbox,
                                 network_available=network_available)
        return None

    def adopt(self, source_dir: Path, gate=None, approve=None, memory=None,
              conversation_id: str = "", tool_posture: str = "standard",
              kernel_mode: bool = False, test_backend_factory=None) -> tuple[bool, list[ScanFinding], str]:
        """Adopt a plugin, in stages: preflight, approval,
        then commit. Returns (ok, findings, message).

        The approve callback, when given, receives (plugin, findings,
        tree_hash, body): the same approved tree hash and card body
        the gate.ask path shows, so a callback approver sees exactly
        what it approved. Older two-argument callbacks
        (plugin, findings) still work via a TypeError fallback.
        No caller in the repo passes a callback today (the WebUI
        route passes approve=None, the CLI passes a gate only);
        the callback path exists for tests and out-of-tree callers.
        Budget accounting is the caller's job: the run tool
        closure acquires the governor between approval and
        commit, so a rejection consumes nothing.
        """
        preflight = preflight_plugin(source_dir)
        if not preflight.ok:
            return False, preflight.findings, preflight.message
        allowed, reason = adoption_gate(
            tool_posture, kernel_mode, preflight.plugin.capabilities,
            sandboxed=any(str(tool.get("mode", "")).lower() == "sandbox"
                          for tool in preflight.plugin.tools))
        if not allowed:
            return False, preflight.findings, f"Adoption refused: {reason}"
        if test_backend_factory is None:
            return False, preflight.findings, (
                "Adoption refused: an isolated, network-disabled acceptance-test "
                "backend is required")
        backend = None
        try:
            from .plugin_acceptance import test_candidate
            backend = test_backend_factory()
            evidence = test_candidate(source_dir, backend)
            if evidence["source_hash"] != preflight.approved_hash:
                return False, preflight.findings, "Adoption refused: candidate changed after preflight"
            # Tell the owner what was validated before asking for approval.
            # Do not include arbitrary argv or test output, which may contain
            # secrets supplied by the plugin author.
            preflight.body += (
                "\nAcceptance validation: "
                f"{len(evidence['tests'])} test command(s) passed in isolated "
                f"{evidence['backend']} with network disabled. Evidence is bound "
                f"to candidate hash {evidence['source_hash']}."
            )
        except Exception as exc:
            return False, preflight.findings, f"Adoption refused: acceptance tests failed: {type(exc).__name__}: {exc}"
        finally:
            if backend is not None:
                backend.cleanup()
        if not ask_plugin_approval(gate, approve, preflight):
            return False, preflight.findings, "Adoption rejected by owner"
        return commit_plugin(self, source_dir, preflight,
                             memory=memory, conversation_id=conversation_id,
                             test_evidence=evidence)
