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

import hashlib
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


def import_skill_file(home, source_path, name: str | None = None) -> Skill:
    """Import a Markdown skill file as a library candidate.

    The file is copied to <home>/skills/<sanitized-name>.md,
    so its stem always matches the trusted-name gate the
    planner reads (skill_texts only admits files whose stem
    is a trusted library name). The description is the
    first non-heading, non-empty line outside any Tests
    section. A ## Tests section, one test command per
    line, becomes the skill's test_commands: without it
    the skill has no tests and can never promote. A skill with no
    borrowed code carries empty code_sources, which passes
    the license gate.

    Re-importing an existing non-retired name is refused:
    retire it first, so trusted content is never silently
    replaced by a new file. Claude-style SKILL.md YAML
    front matter (name and description) is recognized; on
    add failure the copied
    file is removed again, so a failed import leaves no
    orphan discovery file behind.
    """
    import re as _re
    source = Path(source_path)
    if not source.is_file():
        raise ValueError(f"skill file not found: {source_path}")
    text = source.read_text(encoding="utf-8")
    metadata, _body = _skill_front_matter(text)
    raw_name = (name or metadata.get("name") or source.stem).strip()
    if not raw_name:
        raise ValueError("skill name is required")
    clean = _re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name)
    lib = SkillLibrary(home)
    existing = lib.get(clean)
    if existing is not None and existing.state != "retired":
        raise ValueError(
            f"skill {clean!r} already exists ({existing.state}); "
            "retire it first to re-import")
    description = str(metadata.get("description", "")).strip()
    in_tests = False
    in_front_matter = text.startswith("---\n")
    for line in text.splitlines():
        stripped = line.strip()
        if in_front_matter:
            if stripped == "---":
                in_front_matter = False
            continue
        if description:
            break
        if stripped.startswith("## "):
            in_tests = stripped[3:].strip().lower() == "tests"
            continue
        if in_tests:
            continue
        if stripped and not stripped.startswith("#"):
            description = stripped[:200]
            break
    skills_dir = Path(home) / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    target = skills_dir / f"{clean}.md"
    target.write_text(text, encoding="utf-8")
    try:
        return lib.add(Skill(name=clean, description=description,
                             test_commands=parse_test_commands(text)))
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _skill_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Read the small string-only subset of Claude SKILL.md metadata.

    This intentionally avoids a YAML dependency and ignores nested or
    multiline values; skill names and descriptions are plain scalars.
    """
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text or ""
    metadata: dict[str, str] = {}
    end = next((i for i in range(1, len(lines))
                if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text or ""
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"name", "description"}:
            scalar = value.strip()
            if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in "\"'":
                scalar = scalar[1:-1]
            metadata[key.strip()] = scalar
    return metadata, "\n".join(lines[end + 1:])


def parse_test_commands(text: str) -> list[str]:
    """One test command per line from a skill file's ## Tests
    section, until the next ## heading. Fence lines, blank
    lines, and comment lines are skipped. No Tests section
    means no test commands, and a skill with no test
    commands is never promotable: tests are the evidence
    promotion stands on, so they live in the file itself,
    not in a side record."""
    commands = []
    in_tests = False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            if stripped[3:].strip().lower() == "tests":
                in_tests = True
                continue
            if in_tests:
                break
            continue
        if not in_tests:
            continue
        if not stripped or stripped.startswith("#") or \
                stripped.startswith("```"):
            continue
        commands.append(stripped)
    return commands


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

    Skill test commands are shell strings, but the real
    Sandbox.run takes an argv list, never a shell string:
    passing the string straight through would run its
    characters as argv. A real Sandbox therefore gets
    ["sh", "-c", cmd]; doubles keep the old call.
    """
    if hasattr(sandbox, "run"):
        from .sandbox import Sandbox as _RealSandbox
        if isinstance(sandbox, _RealSandbox):
            out = sandbox.run(["sh", "-c", cmd])
        else:
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
                if key == "test_evidence":
                    continue
                lines.append(f"- {key}: {value}")
            lines.append("")
        if self.test_commands:
            lines += ["## Tests", ""] + list(self.test_commands) + [""]
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


def _skill_hash(skill: Skill) -> str:
    """Fingerprint the procedure and tests, excluding lifecycle state."""
    fields = {
        "name": skill.name, "version": skill.version,
        "description": skill.description, "privileged": skill.privileged,
        "capabilities": skill.capabilities, "dependencies": skill.dependencies,
        "test_commands": skill.test_commands, "code_sources": skill.code_sources,
    }
    payload = json.dumps(fields, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _markdown_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""



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
        previous = Skill.from_dict(existing) if existing is not None else None
        if previous is not None and skill.version <= previous.version:
            skill.version = previous.version
        if existing is None:
            # Creation order, persisted: consolidation keeps the
            # newest skill, and JSON key sorting must not be
            # mistaken for creation order.
            skill.seq = max(
                (record.get("seq", 0) for record in self._data["skills"].values()),
                default=0) + 1
        else:
            skill.seq = existing.get("seq", 0)
        changed = previous is not None and _skill_hash(skill) != _skill_hash(previous)
        if changed:
            skill.version = max(skill.version, previous.version + 1)
            skill.state = "candidate"
            skill.provenance = {k: v for k, v in skill.provenance.items()
                                if k != "test_evidence"}
        else:
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
        report is not accepted: the caller must provide passing
        evidence from run_tests for this exact skill content.
        Retired skills are never promoted back by this path.
        """
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        if not isinstance(test_report, dict) or not test_report.get("passed"):
            raise ValueError(f"skill {name!r} has no passing test report")
        if test_report.get("skill_hash") != _skill_hash(skill):
            raise ValueError(f"skill {name!r} test evidence does not match its current contents")
        skill_path = self.home / "skills" / f"{skill.name}.md"
        if test_report.get("markdown_hash", "") != _markdown_hash(skill_path):
            raise ValueError(f"skill {name!r} Markdown changed after testing")
        results = test_report.get("results")
        if (not isinstance(results, list)
                or not any(not result.get("trivial") for result in results)
                or any(not result.get("ok") for result in results
                       if not result.get("trivial"))):
            raise ValueError(f"skill {name!r} has no meaningful passing test evidence")
        if skill.state == "retired":
            raise ValueError(f"skill {name!r} is retired")
        previous_evidence = (skill.provenance or {}).get("test_evidence", {})
        if not isinstance(previous_evidence, dict):
            previous_evidence = {}
        changed_since_validation = (
            previous_evidence.get("skill_hash") != test_report.get("skill_hash")
            or previous_evidence.get("markdown_hash", "")
            != test_report.get("markdown_hash", ""))
        if skill.state == "candidate" or (
                skill.state == "trusted" and changed_since_validation):
            skill.state = "validated"
        skill.provenance = dict(skill.provenance)
        skill.provenance["test_evidence"] = {
            "skill_hash": test_report["skill_hash"],
            "markdown_hash": test_report.get("markdown_hash", ""),
            "ran_at": test_report.get("ran_at", ""),
            "results": results,
        }
        self._update(skill)
        return skill

    def harvest_candidates(self, sandbox, governor=None) -> list[str]:
        """Test and promote candidate skills, bounded by the
        run governor's skill budget.

        The mission loop calls this on successful runs: each
        candidate's own test commands run in the sandbox, and
        a passing report promotes it to validated. A failed
        test leaves the skill a candidate; an exhausted skill
        budget stops the harvest. Nothing here trusts: trust
        still needs owner approval, and validated skills
        never enter plan prompts until trusted. Never raises
        for a single skill's failure; a governor error stops
        the harvest instead of failing the run.
        """
        promoted = []
        for skill in self.list(states=("candidate",)):
            if governor is not None:
                try:
                    governor.acquire("skill")
                except Exception:
                    break
            try:
                report = self.run_tests(skill.name, sandbox)
            except Exception:
                continue
            if not report.get("passed"):
                continue
            try:
                self.promote(skill.name, report)
                promoted.append(skill.name)
            except Exception:
                continue
        return promoted

    def trust(self, name: str, approved: bool = False) -> Skill:
        """Mark a skill trusted. Owner approval is mandatory.

        Trust also requires pinned string dependencies
        (name==version): an unpinned build is not
        reproducible, so it cannot be trusted. Dict
        dependencies are verified-install records, not
        requirements, and are exempt.
        """
        if not approved:
            raise PermissionError(f"trusting skill {name!r} needs owner approval")
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        evidence = (skill.provenance or {}).get("test_evidence", {})
        if not isinstance(evidence, dict):
            evidence = {}
        skill_path = self.home / "skills" / f"{skill.name}.md"
        if (skill.state != "validated"
                or evidence.get("skill_hash") != _skill_hash(skill)
                or evidence.get("markdown_hash", "") != _markdown_hash(skill_path)):
            raise ValueError(f"skill {name!r} must pass tests for its current contents before trust")
        if skill.state == "retired":
            raise ValueError(f"skill {name!r} is retired")
        unpinned = [dep for dep in (skill.dependencies or [])
                    if isinstance(dep, str) and "==" not in dep]
        if unpinned:
            raise ValueError(
                f"trusting skill {name!r} needs pinned dependencies: "
                f"{', '.join(sorted(set(unpinned)))} (use name==version)")
        skill.state = "trusted"
        if not skill_path.exists():
            evidence["markdown_hash"] = hashlib.sha256(
                skill.to_markdown().encode("utf-8")).hexdigest()
            skill.provenance["test_evidence"] = evidence
        self._update(skill)
        self._sync_discovery(skill)
        if _markdown_hash(skill_path) != evidence.get("markdown_hash"):
            raise ValueError(f"skill {name!r} discovery file changed during trust")
        return skill

    def trusted_markdown(self) -> dict[str, str]:
        """Return trusted skill files whose tested Markdown bytes still match."""
        result = {}
        for skill in self.list(states=("trusted",)):
            path = self.home / "skills" / f"{skill.name}.md"
            expected = (skill.provenance or {}).get("test_evidence", {}).get(
                "markdown_hash", "")
            if expected and _markdown_hash(path) == expected:
                result[f"{skill.name}.md"] = path.read_text(encoding="utf-8")
        return result

    def _sync_discovery(self, skill: Skill) -> None:
        """Keep the planner's discovery surface in step with trust.

        The planner discovers skills from <home>/skills/*.md
        files, not from this JSON library, so a vetted skill
        that only lives here would never be used. Trusting a
        skill therefore also publishes its Markdown into the
        discovery directory; retiring one removes it again.
        Only trusted skills are published — candidates and
        validated skills stay unvetted and undiscovered.

        A hand-written procedure file is never overwritten:
        trust publishes the metadata stub only when no file
        exists yet. Overwriting would destroy the procedure
        the trust was meant to approve.
        """
        import re as _re
        skills_dir = self.home / "skills"
        filename = _re.sub(r"[^A-Za-z0-9_.-]+", "-", skill.name) + ".md"
        path = skills_dir / filename
        if skill.state == "trusted":
            skills_dir.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(skill.to_markdown(), encoding="utf-8")
        elif skill.state == "retired":
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def retire(self, name: str, reason: str = "") -> Skill:
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"no skill named {name!r}")
        skill.state = "retired"
        skill.retire_reason = reason
        self._update(skill)
        self._sync_discovery(skill)
        from .capability_hub import invalidate_health
        invalidate_health(self.home, f"skill:{name}")
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
        if skill.state == "retired":
            from .capability_hub import invalidate_health
            invalidate_health(self.home, f"skill:{name}")
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
                self._sync_discovery(skill)
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
        from .sandbox import Sandbox as _Sandbox
        if isinstance(sandbox, _Sandbox) and (
                not sandbox.isolated or sandbox.allow_network):
            skill_path = self.home / "skills" / f"{skill.name}.md"
            return {"passed": False, "results": [],
                    "count": len(skill.test_commands or []),
                    "skill_hash": _skill_hash(skill),
                    "markdown_hash": _markdown_hash(skill_path),
                    "ran_at": _now(),
                    "note": "skill tests require an isolated, network-disabled sandbox"}
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
        skill_path = self.home / "skills" / f"{skill.name}.md"
        report = {"passed": passed, "results": results,
                  "count": len(skill.test_commands or []),
                  "skill_hash": _skill_hash(skill),
                  "markdown_hash": _markdown_hash(skill_path),
                  "ran_at": _now()}
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


    def relevant(self, goal: str, limit: int = 5,
                 states: tuple = ("trusted",)) -> list[Skill]:
        """Skills for a goal: trusted first, then validated, then
        candidate; retired never returned. Within a state, more
        keyword overlap with the goal ranks higher.

        Ordinary recall returns trusted skills only: a
        validated skill is staged for approval, a candidate
        is unvetted. Pass states explicitly for development
        and approval views, where untrusted skills must
        stay visible.
        """
        goal_words = set(re.findall(r"[a-z0-9]+", (goal or "").lower()))
        scored: list[tuple[int, int, Skill]] = []
        for skill in self.list(states=states):
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
