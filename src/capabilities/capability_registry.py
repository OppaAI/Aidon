"""Capability registry: tested builds become reusable tools,
through evidence first and human trust second.

The repo already has lifecycles for plugins (draft,
sandbox-test, security-scan, mandatory approval, adopt)
and skills (candidate, validated, trusted, retirement
on failure). What it did not have is a registry for the
two new Modal-era kinds: capability images (built by
the image builder) and MCP servers built in a sandbox.
A manifest record alone is not a capability: nothing
reads it at plan time, nothing tracks whether it is
trusted, and nothing retires it when it breaks.

Lifecycle, deterministic throughout. No model, Clef
included, promotes anything on its own:

- register: requires non-empty test evidence with every
  entry passed, otherwise it refuses. A registered entry
  starts "validated": tested, but not yet usable by runs.
  Registering alone needs no approval, because a
  validated entry cannot be picked up by a plan.
- promote: validated -> trusted, ONLY with a mandatory
  human approval decision (kind "install", mirroring
  adopt_tool_plugin). Trusted is the only status a run
  may use. Without approval, or on rejection, the entry
  stays validated.
- flag: trusted -> validated, no approval needed. This
  is the safe direction (it removes a capability from
  use) and mirrors maintenance: a failing trusted
  capability drops back until it is re-tested and
  re-promoted.
- retire: validated or trusted -> retired, no approval
  needed, with a recorded reason. A retired name may be
  re-registered with fresh evidence; it may not be
  promoted directly.

Names are unique across non-retired entries, the same
rule as the plugin loader: the registry raises on
collision instead of shadowing.

Evidence honesty, stated plainly because the two kinds
differ. Image evidence is builder-produced: a locked
sandbox from the snapshot passed the test command, so
validated means actually tested. MCP evidence is
caller-supplied (server config, claimed tools, test
results): validated means evidence was supplied and
passed, not that this registry live-checked the server.
Live MCP health checks belong to maintenance and to
triage, not to this record. The helper image_evidence()
turns a builder record into the evidence list so the
caller does not retype it.

Thin scope, deliberately: kind is "image" or "mcp" only.
Plugins and skills keep their existing flows; whether
they migrate into this registry is the local-hub
decision, not something this module assumes.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
REGISTRY_FILENAME = "capabilities.json"
KINDS = ("image", "mcp")

VALIDATED = "validated"
TRUSTED = "trusted"
RETIRED = "retired"


def image_evidence(record: dict) -> list:
    """Evidence list from an image builder record: the test
    command that passed in the locked sandbox at build."""
    return [{
        "command": record.get("test_command", ""),
        "passed": bool(record.get("test_passed", False)),
        "detail": "locked sandbox test passed at build",
    }]


class CapabilityRegistry:
    """Index of validated and trusted capabilities."""

    def __init__(self, home):
        self.home = Path(home)

    def _path(self) -> Path:
        return self.home / REGISTRY_FILENAME

    def _read(self) -> dict:
        path = self._path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, entries: dict) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path().with_suffix(".tmp")
        tmp_path.write_text(json.dumps(entries, indent=2, sort_keys=True))
        os.replace(tmp_path, self._path())

    def get(self, name: str):
        return self._read().get(name)

    def list(self, kind: str | None = None,
             status: str | None = None) -> list:
        entries = self._read().values()
        if kind is not None:
            entries = [e for e in entries if e.get("kind") == kind]
        if status is not None:
            entries = [e for e in entries if e.get("status") == status]
        return sorted(entries, key=lambda e: e.get("name", ""))

    def usable(self) -> list:
        """Trusted entries only: the only ones a run may use."""
        return self.list(status=TRUSTED)

    @staticmethod
    def _check_evidence(test_evidence) -> None:
        if not test_evidence:
            raise ValueError(
                "cannot register an untested capability: "
                "supply passing test evidence")
        for item in test_evidence:
            if not isinstance(item, dict) or not item.get("passed"):
                raise ValueError(
                    "cannot register: test evidence did not all pass")

    @staticmethod
    def _check_mcp_config(config: dict) -> None:
        transport = config.get("transport")
        if transport == "stdio" and not config.get("command"):
            raise ValueError(
                "MCP stdio config needs a command")
        if transport == "http" and not config.get("url"):
            raise ValueError("MCP http config needs a url")
        if transport not in ("stdio", "http"):
            raise ValueError(
                f"unknown MCP transport: {transport!r}")
        if not config.get("tools"):
            raise ValueError(
                "MCP registration needs at least one claimed tool")

    def register(self, name: str, kind: str, config: dict,
                 test_evidence) -> dict:
        """Record a tested capability as validated."""
        if not SAFE_NAME.match(name):
            raise ValueError(f"Invalid capability name: {name!r}")
        if kind not in KINDS:
            raise ValueError(
                f"unknown capability kind: {kind!r} "
                "(thin scope: image, mcp)")
        if kind == "mcp":
            self._check_mcp_config(config)
        self._check_evidence(test_evidence)
        entries = self._read()
        existing = entries.get(name)
        if existing is not None and existing.get("status") != RETIRED:
            raise ValueError(
                f"capability already registered: {name} "
                f"({existing.get('status')})")
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "name": name,
            "kind": kind,
            "status": VALIDATED,
            "config": config,
            "test_evidence": list(test_evidence),
            "created_at": now,
            "updated_at": now,
        }
        entries[name] = entry
        self._write(entries)
        return entry

    def promote(self, name: str, approve) -> dict:
        """Validated -> trusted, only with human approval."""
        entries = self._read()
        entry = entries.get(name)
        if entry is None:
            raise ValueError(f"unknown capability: {name}")
        if entry.get("status") != VALIDATED:
            raise ValueError(
                f"cannot promote {name}: status is "
                f"{entry.get('status')}, not validated")
        if approve is None:
            raise RuntimeError(
                "promotion to trusted needs human approval")
        decision = approve(
            "install", f"Trust capability {name}?",
            f"Kind: {entry.get('kind')}\n"
            f"Test evidence: {entry.get('test_evidence')}")
        verdict = getattr(decision, "verdict", None)
        if verdict not in ("approved", "auto_approved"):
            raise RuntimeError(
                "capability promotion rejected by approval")
        entry["status"] = TRUSTED
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(entries)
        return entry

    def flag(self, name: str, reason: str) -> dict:
        """Trusted -> validated (safe direction, no approval)."""
        entries = self._read()
        entry = entries.get(name)
        if entry is None:
            raise ValueError(f"unknown capability: {name}")
        if entry.get("status") != TRUSTED:
            raise ValueError(
                f"cannot flag {name}: status is "
                f"{entry.get('status')}, not trusted")
        entry["status"] = VALIDATED
        entry["flag_reason"] = reason
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(entries)
        return entry

    def retire(self, name: str, reason: str) -> dict:
        """Validated or trusted -> retired, with a reason."""
        entries = self._read()
        entry = entries.get(name)
        if entry is None:
            raise ValueError(f"unknown capability: {name}")
        if entry.get("status") == RETIRED:
            raise ValueError(f"{name} is already retired")
        entry["status"] = RETIRED
        entry["retire_reason"] = reason
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._write(entries)
        return entry
