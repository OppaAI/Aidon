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

from .plugin_scan import (
    BLOCKING_TYPES,
    MAX_PLUGIN_BYTES,
    NAME_RE,
    SECRET_RE,
    Plugin,
    ScanFinding,
    _capability_policy,
    _existing_approved_plugin,
    adoption_gate,
    load_plugin,
    plugin_is_adopted,
    plugin_tree_hash,
    sandbox_module_paths,
    scan_plugin,
)

# Adoption size cap: a plugin tree larger than this is rejected before
# any approval card, so an oversized or bloated source never reaches
# the owner as something to approve.
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
            or test_evidence.get("backend") not in ("modal", "docker", "bubblewrap")
            or not isinstance(test_evidence.get("tests"), list)
            or not test_evidence["tests"]
            or any(not isinstance(result, dict)
                   or result.get("returncode") != 0
                   for result in test_evidence["tests"])):
        return False, findings, (
            "Adoption refused: verified isolated acceptance-test evidence is required")
    if test_evidence.get("source_hash") != current_hash:
        return False, findings, "Adoption aborted: test evidence does not match candidate"
    already = _existing_approved_plugin(registry, plugin.name, current_hash)
    if already is not None:
        registry.plugins[plugin.name] = already
        return True, findings, f"Plugin {plugin.name} is already adopted at these approved bytes"
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
        existing = _existing_approved_plugin(
            self, preflight.plugin.name, preflight.approved_hash)
        if existing is not None:
            self.plugins[existing.name] = existing
            return True, preflight.findings, (
                f"Plugin {existing.name} is already adopted at these approved bytes")
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
                f"{len(evidence['tests'])} test command(s) and "
                f"{len(evidence.get('mutation_checks', []))} tool mutation check(s) "
                f"passed in isolated {evidence['backend']} with network disabled. "
                "Evidence is bound "
                f"to candidate hash {evidence['source_hash']}.\n"
                "Test quality: each declared tool must be called inside a result "
                "assertion and pass a mutation check. This does not prove complete "
                "coverage or safety; review the candidate test source before approval."
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
