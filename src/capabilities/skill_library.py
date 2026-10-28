"""Full skill library, separate from the lifecycle counter store.

The lifecycle store tracks a skill's state and daily learn cap.
This library is the skill itself: what it does, what it may
reuse, what version it is, what evidence backs it, and how
it is tested. A skill starts as a candidate, becomes
validated when its own tests pass, becomes trusted only
with explicit owner approval, and is retired when it
fails too often or is consolidated away. Retired skills
are kept for the record but never returned by recall.

License gate, the one rule that matters here: code folded
into a skill may only come from permissive licenses.
Copyleft, proprietary, and unknown or missing licenses
are read-only for ideas. Their code is never copied
into a skill, and add() refuses a skill whose declared
code sources fail that gate.

Storage: one JSON file, <home>/skills.json, with the
current skill records and a snapshot history per skill.
A snapshot is taken on add and on every state change,
so rollback() can restore an earlier version's record.
No secrets are stored here.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

STATES = ("candidate", "validated", "trusted", "retired")
_STATE_RANK = {"trusted": 0, "validated": 1, "candidate": 2, "retired": 3}
DEFAULT_MAX_FAILURES = 3

PERMISSIVE_LICENSES = frozenset({
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause",
    "bsd-2-clause-freebsd", "bsd-2-clause-patent", "isc",
})
COPYLEFT_LICENSES = frozenset({
    "gpl-2.0", "gpl-2.0-only", "gpl-2.0-or-later",
    "gpl-3.0", "gpl-3.0-only", "gpl-3.0-or-later",
    "agpl-3.0", "agpl-3.0-only", "agpl-3.0-or-later",
    "lgpl-2.0", "lgpl-2.1", "lgpl-3.0",
    "mpl-2.0", "epl-2.0", "cddl-1.0",
})

_TRIVIAL_PATTERNS = (
    re.compile(r"^\s*(true|:|exit\s+0)\s*$"),
    re.compile(r"^\s*#.*$"),
    re.compile(r"^\s*(echo|printf)\b[^\|;&><]*$"),
    re.compile(r"^\s*python3?\s+-c\s+['\"](pass|print\([^)]*\))['\"]\s*$"),
)
_CHAIN_CHARS = "|;&><"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_license(spdx: str | None) -> str:
    return (spdx or "").strip().lower()


def license_allows_reuse(spdx: str | None) -> tuple[bool, str]:
    """Single chokepoint for the license gate.

    Returns (allowed, reason). Only MIT, Apache-2.0, BSD
    variants, and ISC are reusable. Copyleft, proprietary,
    unknown, and missing licenses are read-only for ideas.
    """
    key = _norm_license(spdx)
    if not key:
        return False, "no license declared - treat as read-only"
    if key in PERMISSIVE_LICENSES:
        return True, f"{spdx} is permissive - reuse allowed"
    if key in COPYLEFT_LICENSES:
        return False, f"{spdx} is copyleft - read-only for ideas, never copy code"
    return False, f"license {spdx!r} is not on the permissive list - read-only"


def check_code_sources(sources: list[dict]) -> None:
    """Raise PermissionError if any declared code source forbids reuse.

    A source is a dict with a license field, under the key
    "license" or "spdx". An empty sources list passes: a skill
    with no borrowed code has nothing to gate.
    """
    for source in sources or []:
        spdx = source.get("license", source.get("spdx"))
        allowed, reason = license_allows_reuse(spdx)
        if not allowed:
            name = source.get("name") or source.get("url") or "unnamed source"
            raise PermissionError(f"code source {name!r} blocked: {reason}")


def is_trivial_test(cmd: str) -> bool:
    """True when a test command cannot fail for a broken skill.

    Shell no-ops, bare output commands, and a python -c that
    only passes or prints a literal prove nothing. A command
    that pipes, chains, or redirects does real work even when
    it starts with echo, so it is never trivial.
    """
    text = cmd or ""
    if any(ch in text for ch in _CHAIN_CHARS):
        return False
    return any(pattern.match(text) for pattern in _TRIVIAL_PATTERNS)


def _sandbox_run(sandbox, cmd: str) -> tuple[bool, str]:
    """Run one command through a sandbox double or a real sandbox.

    Accepts run(cmd) returning a dict with ok/output, an
    object with ok/stdout/stderr, or a run_command(cmd)
    returning a result with returncode/stdout/stderr.
    """
    if hasattr(sandbox, "run"):
        out = sandbox.run(cmd)
        if isinstance(out, dict):
            return bool(out.get("ok")), str(out.get("output", ""))
        if hasattr(out, "ok"):
            text = (getattr(out, "stdout", "") or "") + (getattr(out, "stderr", "") or "")
            return bool(out.ok), text
        return bool(out), str(out)
    if hasattr(sandbox, "run_command"):
        result = sandbox.run_command(cmd)
        ok = getattr(result, "returncode", 1) == 0 and not getattr(result, "timed_out", False)
        text = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
        return ok, text
    raise TypeError("sandbox has neither run() nor run_command()")


@dataclass
class Skill:
    name: str
    description: str = ""
    seq: int = 0
    version: int = 1
    state: str = "candidate"
    privileged: bool = False
    capabilities: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    test_commands: list[str] = field(default_factory=list)
    code_sources: list[dict] = field(default_factory=list)
    failures: int = 0
    retire_reason: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})

    def to_markdown(self) -> str:
        lines = [
            f"# Skill: {self.name}",
            "",
            f"**state:** {self.state} - **version:** v{self.version} - "
            f"**privileged:** {'yes' if self.privileged else 'no'}",
            "",
            self.description or "(no description)",
            "",
        ]
        if self.capabilities:
            lines += ["## Capabilities", ""] + [f"- {cap}" for cap in self.capabilities] + [""]
        if self.dependencies:
            lines += ["## Dependencies", ""] + [f"- {dep}" for dep in self.dependencies] + [""]
        required = self.required_packages()
        if required:
            lines += ["## Requires (install before use, host-side, "
                      "approval-gated)", ""] + [f"- {pkg}" for pkg in required] + [""]
        if self.provenance:
            lines += ["## Provenance", ""]
            for key, value in self.provenance.items():
                lines.append(f"- {key}: {value}")
            lines.append("")
        return "\n".join(lines)

    def required_packages(self) -> list[str]:
        """The python packages this skill needs installed.

        A plain string dependency is a declared package name. A dict
        dependency is the Aidon shape and only counts when it is a
        verified python install: a model-claimed name is a hint, not a
        requirement, and treating it as one would make a skill look
        unprovisioned for a package nobody ever installed.
        """
        out: list[str] = []
        for dep in self.dependencies or []:
            if isinstance(dep, str):
                name = dep
            elif isinstance(dep, dict):
                if dep.get("kind", "python") != "python" or not dep.get("verified"):
                    continue
                name = str(dep.get("name", ""))
            else:
                continue
            name = name.split("==")[0].split("[")[0].strip()
            if name and name not in out:
                out.append(name)
        return out



class SkillLibrary:
    """Persisted skill library with version history."""

    def __init__(self, home, max_failures: int = DEFAULT_MAX_FAILURES):
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "skills.json"
        self.max_failures = max_failures
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"skills": {}, "versions": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"skills": {}, "versions": {}}
        if not isinstance(data, dict):
            return {"skills": {}, "versions": {}}
        data.setdefault("skills", {})
        data.setdefault("versions", {})
        return data

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")

    def _refresh(self) -> None:
        # The file is the source of truth, not this instance's
        # memory: a second instance (another process, or the
        # learn cycle's own library) may have written since
        # this one loaded. Reads refresh, so get() never
        # returns a state another writer already changed.
        self._data = self._load()

    def _snapshot(self, skill: Skill) -> None:
        history = self._data["versions"].setdefault(skill.name, [])
        record = skill.to_dict()
        record["snapshot_at"] = _now()
        history.append(record)

    def add(self, skill: Skill, approved: bool = False) -> Skill:
        """Add a skill, enforcing the license gate first.

        A privileged skill still starts as a candidate; trust()
        with approval is what promotes it to trusted, never add().
        Re-adding an existing name with a higher version replaces
        the record and snapshots the new version.
        """
        check_code_sources(skill.code_sources)
        if skill.state not in STATES:
            raise ValueError(f"unknown skill state: {skill.state!r}")
        self._refresh()
        existing = self._data["skills"].get(skill.name)
        if existing is not None and skill.version <= existing["version"]:
            skill.version = existing["version"]
        if existing is None:
            # Creation order, persisted: consolidation keeps the
            # newest skill, and JSON key sorting must not be
            # mistaken for creation order.
            skill.seq = max(
                (record.get("seq", 0) for record in self._data["skills"].values()),
                default=0) + 1
        else:
            skill.seq = existing.get("seq", 0)
        skill.state = "candidate" if existing is None else existing["state"]
        if existing is not None and skill.state == "retired":
            skill.state = "candidate"
        skill.updated_at = _now()
        self._data["skills"][skill.name] = skill.to_dict()
        self._snapshot(skill)
        self._save()
        return skill

    def get(self, name: str) -> Skill | None:
        self._refresh()
        record = self._data["skills"].get(name)
        return Skill.from_dict(record) if record else None

    def list(self, states: tuple = ("candidate", "validated", "trusted")) -> list[Skill]:
        self._refresh()
        return [Skill.from_dict(record) for record in self._data["skills"].values()
                if record.get("state") in states]

    def version_history(self, name: str) -> list[dict]:
        self._refresh()
        return list(self._data["versions"].get(name, []))

    def _update(self, skill: Skill) -> Skill:
        skill.updated_at = _now()
        self._data["skills"][skill.name] = skill.to_dict()
        self._snapshot(skill)
        self._save()
        return skill

    def promote(self, name: str, test_report: dict | None = None) -> Skill:
        """candidate -> validated, only on a passing test report.

        A report that did not pass refuses the promotion. No
        report means the caller already verified the tests.
        Retired skills are never promoted back by this path.
        """
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        if test_report is not None and not test_report.get("passed"):
            raise ValueError(f"skill {name!r} has no passing test report")
        if skill.state == "retired":
            raise ValueError(f"skill {name!r} is retired")
        if skill.state == "candidate":
            skill.state = "validated"
            self._update(skill)
        return skill

    def trust(self, name: str, approved: bool = False) -> Skill:
        """Mark a skill trusted. Owner approval is mandatory."""
        if not approved:
            raise PermissionError(f"trusting skill {name!r} needs owner approval")
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        if skill.state == "retired":
            raise ValueError(f"skill {name!r} is retired")
        skill.state = "trusted"
        self._update(skill)
        return skill

    def retire(self, name: str, reason: str = "") -> Skill:
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        skill.state = "retired"
        skill.retire_reason = reason
        self._update(skill)
        return skill

    def record_failure(self, name: str) -> Skill | None:
        skill = self.get(name)
        if skill is None:
            return None
        if skill.state == "retired":
            return skill
        skill.failures += 1
        if skill.failures >= self.max_failures:
            skill.state = "retired"
            skill.retire_reason = f"failed {skill.failures} times"
        self._update(skill)
        return skill

    def rollback(self, name: str, version: int) -> Skill:
        """Restore the record as first snapshotted at that version.

        Version history holds one snapshot per state change, so
        the first snapshot carrying the version is the record
        as it stood when that version was created.
        """
        self._refresh()
        history = self._data["versions"].get(name, [])
        for snapshot in history:
            if snapshot.get("version") == version:
                record = {key: value for key, value in snapshot.items()
                          if key != "snapshot_at"}
                skill = Skill.from_dict(record)
                self._data["skills"][name] = skill.to_dict()
                self._snapshot(skill)
                self._save()
                return skill
        raise KeyError(f"no version {version} for skill {name!r}")

    def run_tests(self, name: str, sandbox) -> dict:
        """Run the skill's own test commands in the sandbox.

        Returns {"passed", "results", "count"}. No commands means
        not promotable. Trivial commands are reported but never
        counted: a skill whose only tests are trivial has no
        tests. Passed needs at least one meaningful command
        and all meaningful commands ok.

        The skill's verified python dependencies are pre-flighted
        first. The sandbox is offline, so a skill that needs a
        package can only be tested when that package is already
        present; when it is not, the report says which package is
        missing, because "test failed" hides the difference between
        a broken skill and an unprovisioned one.
        """
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        results: list[dict] = []
        meaningful = 0
        meaningful_ok = True
        missing = self._missing_dependencies(skill, sandbox)
        for cmd in skill.test_commands or []:
            if is_trivial_test(cmd):
                results.append({"cmd": cmd, "ok": False, "trivial": True,
                                "output": "trivial command - proves nothing, not counted"})
                continue
            meaningful += 1
            try:
                ok, output = _sandbox_run(sandbox, cmd)
            except Exception as exc:
                ok, output = False, f"{type(exc).__name__}: {exc}"
            results.append({"cmd": cmd, "ok": ok, "output": output[:500]})
            meaningful_ok = meaningful_ok and ok
        passed = meaningful > 0 and meaningful_ok and not missing
        report = {"passed": passed, "results": results,
                  "count": len(skill.test_commands or []), "ran_at": _now()}
        if missing:
            report["missing_dependencies"] = missing
            report["note"] = (
                "the skill needs packages that are not installed in this "
                "sandbox (" + ", ".join(item["name"] for item in missing)
                + "). It is not broken, it is unprovisioned: install them "
                "with install_python_package (host-side, approval-gated) "
                "and re-run its tests.")
        return report

    def _missing_dependencies(self, skill: Skill, sandbox) -> list[dict]:
        """Which required python dependencies are unavailable here.

        The mission venv (.venv in the run workdir) is checked first,
        because that is where an approved install lands, then the
        system interpreter.
        """
        packages = skill.required_packages()
        if not packages:
            return []
        interpreters = []
        workdir = getattr(sandbox, "workdir", None)
        if workdir is not None:
            venv_python = Path(workdir) / ".venv" / "bin" / "python"
            if venv_python.exists():
                interpreters.append(str(venv_python))
        interpreters.append("python3")
        missing: list[dict] = []
        for package in packages:
            module = package.replace("-", "_")
            found = False
            for interpreter in interpreters:
                try:
                    ok, _ = _sandbox_run(
                        sandbox, f'{interpreter} -c "import {module}"')
                except Exception:
                    ok = False
                if ok:
                    found = True
                    break
            if not found:
                missing.append({"kind": "python", "name": package})
        return missing


    def relevant(self, goal: str, limit: int = 5) -> list[Skill]:
        """Skills for a goal: trusted first, then validated, then
        candidate; retired never returned. Within a state, more
        keyword overlap with the goal ranks higher.
        """
        goal_words = set(re.findall(r"[a-z0-9]+", (goal or "").lower()))
        scored: list[tuple[int, int, Skill]] = []
        for skill in self.list():
            haystack = " ".join([skill.name, skill.description] + skill.capabilities).lower()
            words = set(re.findall(r"[a-z0-9]+", haystack))
            overlap = len(goal_words & words)
            scored.append((_STATE_RANK[skill.state], -overlap, skill))
        scored.sort(key=lambda item: (item[0], item[1], item[2].name))
        return [skill for _, _, skill in scored[:limit]]

    def render(self, skills: list[Skill]) -> str:
        if not skills:
            return "No skills."
        lines = []
        for skill in skills:
            lines.append(f"- {skill.name} (v{skill.version}, {skill.state}): "
                         f"{skill.description or '(no description)'}")
        return "\n".join(lines)
