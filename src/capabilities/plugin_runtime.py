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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


CAPABILITY_HTTP = "http"
CAPABILITY_FS = "fs"
CAPABILITY_RUN = "run"
CAPABILITY_SECRET = "secret"

KNOWN_CAPABILITIES = (CAPABILITY_HTTP, CAPABILITY_FS, CAPABILITY_RUN,
                      CAPABILITY_SECRET)


@dataclass
class CapabilityPolicy:
    """The exact power surface a plugin was approved for."""

    http: bool = False
    allowed_hosts: tuple[str, ...] = ()
    fs: bool = False
    run: bool = False
    secrets: tuple[str, ...] = ()

    @classmethod
    def from_capabilities(cls, capabilities) -> "CapabilityPolicy":
        """Parse manifest capability strings, rejecting unknown ones.

        IVALID input is a hard error: a plugin that asks for
        ``"netwrok"`` must not silently get nothing, because the
        owner would then approve a card that understates the tool.
        """
        http = False
        fs = False
        run = False
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
            elif name == CAPABILITY_SECRET:
                if not arg:
                    raise ValueError(
                        "secret capability needs a name: secret:NAME")
                secrets.append(arg)
            else:
                raise ValueError(f"unknown plugin capability: {cap}")
        return cls(http=http, allowed_hosts=tuple(hosts), fs=fs,
                   run=run, secrets=tuple(secrets))

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
