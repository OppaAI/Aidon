"""Skill source import, test parsing, and code provenance gates."""

from __future__ import annotations

import re
from pathlib import Path

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
    from .skill_library import Skill, SkillLibrary
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


