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
  Plugins live in <home>/plugins/<name>/, never in the
  repo, never in a central service.

Adoption, in order, no step skipped:
1. Scan the plugin directory. The scan reports path,
   line, and type only, never values, never a secret
   itself, and never an invented CVE id.
2. Blocking findings reject adoption, even if the user
   would approve: eval, exec, subprocess or os.system,
   socket or network calls, and a secret assignment
   (a key, token, or password set to a string value).
3. Non-blocking warnings are shown on the adoption card:
   a file write in plugin code, and a dependency that
   is *, latest, or unpinned. Warnings do not block,
   they are disclosed, and adoption still needs approval.
4. The owner approves on a decision card, with the scan
   report as the card body. No approval, no adoption.
5. Only then is the plugin copied to <home>/plugins/,
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

BLOCKING_TYPES = ("eval_exec", "subprocess", "network", "secret")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
SECRET_RE = re.compile(r"(api[_-]?key|password|token|secret)", re.IGNORECASE)


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

    def tool_names(self) -> list[str]:
        return [str(t.get("name", "")) for t in self.tools if t.get("name")]


def scan_plugin(directory: Path) -> list[ScanFinding]:
    """Security scan, path/line/type only, never values."""
    findings: list[ScanFinding] = []
    directory = Path(directory)
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
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = ""
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name in ("eval", "exec"):
                    findings.append(ScanFinding(rel, node.lineno, "eval_exec",
                                                "eval or exec call"))
                elif name in ("system", "popen") or (isinstance(func, ast.Attribute) and name in ("Popen", "run") and isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    findings.append(ScanFinding(rel, node.lineno, "subprocess",
                                                "Subprocess or shell call"))
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
            if isinstance(node, ast.ImportFrom) and node.module in ("socket", "subprocess", "urllib.request"):
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
                    skills=list(manifest.get("skills", [])))
    for tool in plugin.tools:
        module_name = str(tool.get("module", ""))
        func_name = str(tool.get("function", ""))
        tool_name = str(tool.get("name", ""))
        if not module_name or not func_name or not tool_name:
            raise ValueError("plugin tool needs name, module, and function")
        module_path = directory / module_name
        if not module_path.exists():
            raise ValueError(f"plugin module missing: {module_name}")
        if not execute:
            # Static check only: no import, no top-level code runs.
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
    """Deterministic sha256 over a plugin source tree.

    Covers everything copytree would copy: every entry's relative
    path (directories included, so a structural change is detected)
    and every file's bytes, in sorted relative-path order. Nothing
    the copy would copy is skipped, so the hash binds an approval
    to exactly the bytes that get adopted.
    """
    source_dir = Path(source_dir)
    digest = hashlib.sha256()
    for path in sorted(source_dir.rglob("*"),
                       key=lambda p: str(p.relative_to(source_dir))):
        rel = str(path.relative_to(source_dir))
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


class PluginRegistry:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.plugins_dir = self.home / "plugins"
        self.plugins: dict[str, Plugin] = {}
        self.skills_dir = self.home / "skills"

    def load_all(self) -> list[Plugin]:
        """Load every valid plugin in <home>/plugins/, skip invalid ones."""
        self.plugins = {}
        if not self.plugins_dir.exists():
            return []
        for path in sorted(self.plugins_dir.iterdir()):
            if not path.is_dir():
                continue
            try:
                plugin = load_plugin(path)
                self.plugins[plugin.name] = plugin
            except Exception:
                continue
        return list(self.plugins.values())

    def tool_names(self) -> list[str]:
        names: list[str] = []
        for plugin in self.plugins.values():
            names.extend(plugin.tool_names())
        return sorted(names)

    def skill_texts(self) -> dict[str, str]:
        texts: dict[str, str] = {}
        for plugin in self.plugins.values():
            for skill in plugin.skills:
                path = plugin.directory / str(skill)
                texts[f"{plugin.name}/{skill}"] = path.read_text(encoding="utf-8")
        # Standalone skills in <home>/skills/, Markdown only.
        if self.skills_dir.exists():
            for path in sorted(self.skills_dir.glob("*.md")):
                texts[path.name] = path.read_text(encoding="utf-8")
        return texts

    def call(self, name: str, args: dict) -> str:
        for plugin in self.plugins.values():
            if name in plugin.functions:
                result = plugin.functions[name](dict(args))
                return str(result)
        raise ValueError(f"unknown plugin tool: {name}")

    def adopt(self, source_dir: Path, gate=None, approve=None, memory=None,
              conversation_id: str = "") -> tuple[bool, list[ScanFinding], str]:
        """Adopt a plugin: scan, approve, then copy. Returns (ok, findings, message).

        The approve callback, when given, receives (plugin, findings,
        tree_hash, body): the same approved tree hash and card body
        the gate.ask path shows, so a callback approver sees exactly
        what it approved. Older two-argument callbacks
        (plugin, findings) still work via a TypeError fallback.
        No caller in the repo passes a callback today (the WebUI
        route passes approve=None, the CLI passes a gate only);
        the callback path exists for tests and out-of-tree callers.
        """
        source_dir = Path(source_dir)
        findings = scan_plugin(source_dir)
        blocking = [f for f in findings if f.type in BLOCKING_TYPES]
        if blocking:
            return False, findings, "Adoption rejected: blocking scan findings"
        # Size cap, before any approval card: an oversized source is
        # not something the owner should be asked to approve.
        total_size = plugin_tree_size(source_dir)
        if total_size > MAX_PLUGIN_BYTES:
            return False, findings, (
                f"Adoption rejected: plugin source is {total_size} bytes, "
                f"over the {MAX_PLUGIN_BYTES} byte cap")
        # Approval binding: hash the approved bytes now, re-hash
        # immediately before the copy, and abort if they changed.
        try:
            approved_hash = plugin_tree_hash(source_dir)
        except OSError as exc:
            return False, findings, f"Adoption rejected: cannot hash source: {exc}"
        # Full security gate over the plugin's own code: a fail
        # verdict (os.system, eval/exec, piped download-to-shell,
        # secret-like strings) blocks adoption even when the
        # pattern scan above passed; review verdicts go on the
        # approval card below.
        gate_findings = []
        try:
            from .security_gate import gate_text
            for py_path in sorted(source_dir.rglob("*.py")):
                try:
                    result = gate_text(str(py_path), py_path.read_text(encoding="utf-8"))
                except OSError:
                    continue
                gate_findings.extend(result.findings)
                if result.verdict == "fail":
                    return False, findings, "Adoption rejected: security gate fail"
        except Exception as exc:
            # Fail closed: a gate that failed to run must never present
            # as a clean scan. The old code reset findings to [] and
            # proceeded to an approval card showing "no findings".
            return False, findings, f"Adoption rejected: security gate error: {exc}"
        try:
            # Static validation only. Importing the module here would
            # run its top-level code on the host before the owner has
            # seen the scan report or approved anything.
            plugin = load_plugin(source_dir, execute=False)
        except Exception as exc:
            return False, findings, f"Adoption rejected: invalid plugin: {exc}"
        # Approval card: scan report as body, path/line/type only.
        body_lines = [f"Plugin: {plugin.name} {plugin.version}",
                      f"Description: {plugin.description}",
                      f"Tools: {', '.join(plugin.tool_names()) or 'none'}",
                      f"Skills: {', '.join(plugin.skills) or 'none'}",
                      f"Approved source hash: {approved_hash}"]
        if findings:
            body_lines.append("Scan warnings:")
            body_lines.extend(f"  {f.path}:{f.line} {f.type}" for f in findings)
        else:
            body_lines.append("Scan: no findings")
        if gate_findings:
            body_lines.append("Security gate findings:")
            body_lines.extend(f"  {f.severity} {f.source}: {f.message}" for f in gate_findings)
        body = "\n".join(body_lines)
        approved = False
        if approve is not None:
            # Callback approvers see exactly what the gate.ask path
            # shows: the approved tree hash and the card body, so
            # the approval binds to the same bytes. Older
            # two-argument callbacks keep working via fallback.
            try:
                approved = bool(approve(plugin, findings, approved_hash, body))
            except TypeError:
                approved = bool(approve(plugin, findings))
        elif gate is not None:
            # Gates from out-of-tree callers and test doubles may take
            # only (kind, title, body). The action_class is an
            # autonomy hint, not part of the decision, so a plain gate
            # is asked without it rather than crashing adoption.
            try:
                decision = gate.ask(
                    "plugin",
                    "Adopt this plugin?",
                    body,
                    action_class="plugin",
                )
            except TypeError:
                decision = gate.ask("plugin", "Adopt this plugin?", body)
            approved = decision.verdict in ("approved", "auto_approved")
        if not approved:
            return False, findings, "Adoption rejected by owner"
        # Re-hash immediately before the copy, before touching any
        # existing install: if the source changed after approval, the
        # approval no longer binds to these bytes, so abort and leave
        # any previously installed version untouched.
        try:
            current_hash = plugin_tree_hash(source_dir)
        except OSError as exc:
            return False, findings, f"Adoption aborted: cannot re-hash source: {exc}"
        if current_hash != approved_hash:
            return False, findings, (
                "Adoption aborted: plugin source changed after approval "
                f"(approved hash {approved_hash}, current hash {current_hash}); "
                "approve the new source to adopt it")
        target = self.plugins_dir / plugin.name
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source_dir, target)
        self.plugins[plugin.name] = load_plugin(target)
        if memory is not None:
            try:
                memory.record_episodic(
                    conversation_id, f"Adopted plugin {plugin.name}",
                    {"plugin": plugin.name, "version": plugin.version,
                     "tools": plugin.tool_names()})
            except Exception:
                pass
        return True, findings, f"Adopted plugin {plugin.name}"
