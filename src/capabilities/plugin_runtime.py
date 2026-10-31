"""Runtime broker for capability-declared plugin tools.

Pure tools (no ``capabilities`` in the manifest) keep the original
contract: ``def fn(args: dict) -> str``. A tool whose manifest entry
sets ``"context": true`` receives ``fn(args, ctx)`` where ``ctx`` is a
broker exposing only the capabilities the manifest declared and the
owner approved.

The broker is the sandbox boundary. Network, filesystem, subprocess,
and secrets never enter plugin source as raw imports, because those
imports are exactly what the adoption scan blocks. Instead the plugin
calls a broker method, and this module enforces the declared policy:

- ``http`` / ``http:host1,host2`` -> ``ctx.http_get``, ``ctx.http_post``
- ``fs`` -> ``ctx.read_file``, ``ctx.write_file`` (plugin data dir only)
- ``run`` -> ``ctx.run`` (a real sandbox command, never the host shell)
- ``secret:NAME`` -> ``ctx.secret("NAME")``

A capability is granted only when it appears in the manifest. The
approval card lists every declared capability, so the owner approves
the exact power surface before any of it exists.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


CAPABILITY_HTTP = "http"
CAPABILITY_FS = "fs"
CAPABILITY_RUN = "run"
CAPABILITY_SECRET = "secret"
CAPABILITY_NETWORK = "network"

KNOWN_CAPABILITIES = (CAPABILITY_HTTP, CAPABILITY_FS, CAPABILITY_RUN,
                      CAPABILITY_SECRET, CAPABILITY_NETWORK)


@dataclass
class CapabilityPolicy:
    """The exact power surface a plugin was approved for."""

    http: bool = False
    allowed_hosts: tuple[str, ...] = ()
    fs: bool = False
    run: bool = False
    network: bool = False
    secrets: tuple[str, ...] = ()

    @classmethod
    def from_capabilities(cls, capabilities) -> "CapabilityPolicy":
        """Parse manifest capability strings, rejecting unknown ones.

        Invalid input is a hard error: a plugin that asks for
        ``"netwrok"`` must not silently get nothing, because the
        owner would then approve a card that understates the tool.
        """
        http = False
        fs = False
        run = False
        network = False
        hosts: list[str] = []
        secrets: list[str] = []
        for raw in capabilities or ():
            cap = str(raw).strip()
            if not cap:
                continue
            name, _, arg = cap.partition(":")
            name = name.strip().lower()
            arg = arg.strip()
            if name == CAPABILITY_HTTP:
                http = True
                if arg:
                    hosts.extend(h.strip().lower()
                                 for h in arg.split(",") if h.strip())
            elif name == CAPABILITY_FS:
                fs = True
            elif name == CAPABILITY_RUN:
                run = True
            elif name == CAPABILITY_NETWORK:
                network = True
            elif name == CAPABILITY_SECRET:
                if not arg:
                    raise ValueError(
                        "secret capability needs a name: secret:NAME")
                secrets.append(arg)
            else:
                raise ValueError(f"unknown plugin capability: {cap}")
        return cls(http=http, allowed_hosts=tuple(hosts), fs=fs,
                   run=run, network=network, secrets=tuple(secrets))

    def describe(self) -> str:
        parts: list[str] = []
        if self.http:
            where = (", ".join(self.allowed_hosts)
                     if self.allowed_hosts else "any host")
            parts.append(f"http ({where})")
        if self.fs:
            parts.append("fs (plugin data dir)")
        if self.run:
            parts.append("run (sandboxed)")
        if self.network:
            parts.append("network (sandbox only)")
        for secret in self.secrets:
            parts.append(f"secret:{secret}")
        return ", ".join(parts) or "none (pure tool)"


class PluginContext:
    """Broker handed to a context-aware plugin tool.

    Every method checks the policy again at call time. The adoption
    scan is a pre-check, not the enforcement point: a plugin loaded
    from disk could have been edited after adoption, so the policy
    remains the boundary.
    """

    def __init__(self, plugin_name: str, home: Path,
                 policy: CapabilityPolicy, sandbox=None,
                 network_available: bool | None = None):
        self.plugin_name = str(plugin_name)
        self.home = Path(home)
        self.policy = policy
        self.data_dir = self.home / "plugin_data" / self.plugin_name
        self.sandbox = sandbox
        self.network_available = network_available

    # -- http ---------------------------------------------------------

    def _checked_url(self, url) -> str:
        if not self.policy.http:
            raise PermissionError(
                "plugin did not declare the http capability")
        parsed = urllib.parse.urlparse(str(url))
        if parsed.scheme not in ("http", "https"):
            raise ValueError("only http/https URLs are allowed")
        host = (parsed.hostname or "").lower()
        if self.policy.allowed_hosts and host not in self.policy.allowed_hosts:
            raise PermissionError(
                f"host not allowed by plugin capability: {host}")
        return str(url)

    def http_get(self, url, headers=None, timeout=30) -> str:
        target = self._checked_url(url)
        request = urllib.request.Request(
            target, headers=dict(headers or {}), method="GET")
        with urllib.request.urlopen(request, timeout=float(timeout)) as resp:
            return resp.read().decode("utf-8", errors="replace")

    def http_post(self, url, data=None, headers=None, timeout=30) -> str:
        target = self._checked_url(url)
        body = data
        merged_headers = dict(headers or {})
        if isinstance(data, (dict, list)):
            body = json.dumps(data).encode("utf-8")
            merged_headers.setdefault("Content-Type", "application/json")
        elif isinstance(data, str):
            body = data.encode("utf-8")
        request = urllib.request.Request(
            target, data=body, headers=merged_headers, method="POST")
        with urllib.request.urlopen(request, timeout=float(timeout)) as resp:
            return resp.read().decode("utf-8", errors="replace")

    # -- filesystem ---------------------------------------------------

    def _data_path(self, rel) -> Path:
        target = (self.data_dir / str(rel)).resolve()
        root = self.data_dir.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path outside plugin data dir: {rel}")
        return target

    def read_file(self, rel) -> str:
        if not self.policy.fs:
            raise PermissionError("plugin did not declare the fs capability")
        return self._data_path(rel).read_text(encoding="utf-8")

    def write_file(self, rel, text) -> str:
        if not self.policy.fs:
            raise PermissionError("plugin did not declare the fs capability")
        target = self._data_path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(text), encoding="utf-8")
        return f"wrote {rel}"

    # -- subprocess ---------------------------------------------------

    def run(self, command) -> str:
        if not self.policy.run:
            raise PermissionError("plugin did not declare the run capability")
        if self.sandbox is None:
            raise RuntimeError("no sandbox available for plugin run capability")
        if not isinstance(command, list) or not command:
            raise ValueError("command must be a non-empty list")
        result = self.sandbox.run([str(c) for c in command])
        if not result.ok:
            raise RuntimeError(f"command failed: {result.stderr[:200]}")
        return result.stdout

    # -- secrets ------------------------------------------------------

    def secret(self, name) -> str:
        name = str(name)
        if name not in self.policy.secrets:
            raise PermissionError(f"secret not granted to plugin: {name}")
        return os.environ.get(name, "")


def build_context(plugin_name: str, home: Path, capabilities,
                  sandbox=None, network_available: bool | None = None) -> PluginContext:
    """Build a broker for one plugin from its declared capabilities."""
    policy = CapabilityPolicy.from_capabilities(capabilities)
    return PluginContext(plugin_name, home, policy, sandbox=sandbox,
                         network_available=network_available)


# --- Tier 2: sandboxed process tools ---------------------------------

_SANDBOX_RUNNER = r'''
"""Sandbox tool runner: JSON in on stdin, JSON out in result.json.

This process is the isolation boundary. The plugin module is imported
here, never in Aidon's process, so subprocess/network/eval in tool code
stay inside the sandbox. The result goes to a file so a plugin's own
prints cannot corrupt the protocol.
"""
import importlib.util
import json
import os
import sys


def main():
    payload = json.load(sys.stdin)
    args = payload.get("args") or {}
    env = payload.get("env") or {}
    os.environ.update({str(k): str(v) for k, v in env.items()})
    module_path, func_name = sys.argv[1], sys.argv[2]
    sys.path.insert(0, os.path.dirname(os.path.abspath(module_path)))
    try:
        spec = importlib.util.spec_from_file_location("plugin_tool", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        func = getattr(module, func_name)
        result = func(args)
        data = {"result": "" if result is None else str(result)}
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        data = {"error": f"{type(exc).__name__}: {exc}"}
    with open("result.json", "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    if "error" in data:
        sys.exit(1)


if __name__ == "__main__":
    main()
'''

_MAX_KEPT_RUNS = 20


def _prune_runs(runs_root: Path, keep: int = _MAX_KEPT_RUNS) -> None:
    """Keep the newest ``keep`` run dirs; drop the rest. Best effort."""
    try:
        dirs = sorted((p for p in runs_root.iterdir() if p.is_dir()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for old in dirs[keep:]:
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        return


def run_sandbox_tool(plugin, spec: dict, args: dict, sandbox, home: Path,
                     network_available: bool | None = None) -> str:
    """Run a ``"mode": "sandbox"`` tool as an isolated subprocess.

    The plugin module is copied into a fresh run directory and imported
    there by ``runner.py``; Aidon's process never imports it. Args and
    approved secrets travel on stdin as JSON, so they never appear on
    the host command line. Network is off unless the manifest declares
    ``network`` and the owner approved the plugin.
    """
    from .sandbox import Sandbox

    plugin_dir = getattr(plugin, "directory", None)
    if plugin_dir is None:
        raise ValueError("sandbox tool has no plugin directory")
    # Defense in depth: the registry already refuses an unadopted
    # sandbox tool, but the runner repeats the check so no caller can
    # reach process execution without an approval marker.
    if not (Path(plugin_dir) / ".adopted").is_file():
        raise ValueError("sandbox tool plugin is not adopted")
    module_name = str(spec.get("module", ""))
    func_name = str(spec.get("function", ""))
    if not module_name or not func_name:
        raise ValueError("sandbox tool needs module and function")
    timeout = int(spec.get("timeout", 120) or 120)
    policy = CapabilityPolicy.from_capabilities(
        getattr(plugin, "capabilities", []))
    secrets = {name: os.environ.get(name, "") for name in policy.secrets}

    plugin_name = str(getattr(plugin, "name", "plugin"))
    runs_root = Path(home) / "plugin_runs" / plugin_name
    runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = runs_root / uuid.uuid4().hex
    run_dir.mkdir()
    code_dir = run_dir / "code"
    shutil.copytree(
        plugin_dir, code_dir,
        ignore=shutil.ignore_patterns(".adopted", "__pycache__", "*.pyc"))
    (run_dir / "runner.py").write_text(_SANDBOX_RUNNER, encoding="utf-8")

    # Reuse the run sandbox's isolation posture, but give this tool its
    # own workdir and its own network policy.
    backend = "auto"
    require_isolation = False
    if sandbox is not None:
        backend = ("bubblewrap" if getattr(sandbox, "bwrap", None)
                   else "subprocess")
        require_isolation = bool(getattr(sandbox, "require_isolation", False))
    box = Sandbox(run_dir, timeout_seconds=timeout, backend=backend,
                  allow_network=policy.network,
                  require_isolation=require_isolation)

    payload = json.dumps({"args": args, "env": secrets})
    result = box.run(
        [sys.executable, "runner.py",
         str(Path("code") / module_name), func_name],
        input_text=payload)
    out_path = run_dir / "result.json"
    data = None
    if out_path.is_file():
        try:
            data = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = None
    if data is not None:
        _prune_runs(runs_root)
        if "error" in data:
            raise RuntimeError(str(data["error"]))
        return str(data.get("result", ""))
    detail = (result.stderr or result.stdout or "").strip()[:300]
    _prune_runs(runs_root)
    raise RuntimeError(
        f"sandbox tool {func_name} failed: {detail or 'no output'}")
