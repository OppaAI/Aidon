"""Privileged host-side installer.

Everything here runs OUTSIDE the sandbox, on the real host, and ALWAYS
requires explicit human approval (kind="install") — no exceptions, even
in autonomous mode. The approval gate lives in the tool wrappers;
this module only implements the mechanics.

- uv_venv(path): create an isolated venv (default: <workdir>/.venv)
- install_python_package(packages, venv): `uv pip install` into a venv
- apt_install(packages): `sudo apt-get install -y` (needs sudo configured)

Rules enforced here, not just in the prompt:
- package names are strictly validated (no URLs, no shell metachars,
  no version-spec tricks) — anything else is refused before approval
- never installs into Aidon's own venv (a bad dependency must not be
  able to break Aidon itself)
- sudo is probed with `sudo -n true` first; if a password would be
  needed, fail with a clear message (Aidon never handles passwords)
- no shell=True anywhere; argument lists only
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

_PYPI_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.\-]*(\[[A-Za-z0-9_,\-]+\])?(==[A-Za-z0-9_.\-]+)?$")
_APT_RE = re.compile(r"^[a-z0-9][a-z0-9+.\-]*$")


def find_uv() -> str | None:
    """Locate the uv binary, or None."""
    found = shutil.which("uv")
    if found:
        return found
    cand = Path.home() / ".local" / "bin" / "uv"
    return str(cand) if cand.is_file() else None


def _is_aidon_venv(path: Path) -> bool:
    """True if `path` is (or is inside) the Python running Aidon."""
    try:
        return Path(sys.prefix).resolve() in list(
            Path(path).resolve().parents) + [Path(path).resolve()]
    except OSError:
        return False


def _run(argv: list[str], timeout: int = 300) -> tuple[bool, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return False, f"not found: {argv[0]}"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s: {' '.join(argv)}"
    out = (p.stdout + p.stderr)[-3000:]
    return p.returncode == 0, out or "(no output)"


def validate_pypi(packages: list[str]) -> str | None:
    """Return an error string, or None if every name is a plain PyPI spec."""
    for name in packages:
        if not _PYPI_RE.match(name):
            return (f"refused {name!r}: not a plain package spec "
                    f"(name[extras]==version only — no URLs, no flags)")
    return None


def validate_apt(packages: list[str]) -> str | None:
    for name in packages:
        if not _APT_RE.match(name):
            return (f"refused {name!r}: not a plain Debian package name")
    return None


def uv_venv(venv_path: str | Path) -> tuple[bool, str]:
    uv = find_uv()
    if not uv:
        return False, ("uv not found — run ./setup.sh or install uv, "
                       "then retry")
    venv_path = Path(venv_path)
    if _is_aidon_venv(venv_path):
        return False, "refused: that is Aidon's own environment"
    return _run([uv, "venv", str(venv_path)])


def install_python_package(packages: list[str],
                           venv_path: str | Path) -> tuple[bool, str]:
    err = validate_pypi(packages)
    if err:
        return False, err
    venv_path = Path(venv_path)
    if _is_aidon_venv(venv_path):
        return False, ("refused: refusing to install into Aidon's own "
                       "environment — use an isolated mission venv")
    uv = find_uv()
    if not uv:
        return False, "uv not found — run ./setup.sh first"
    vpy = venv_path / "bin" / "python"
    if not vpy.is_file():
        ok, out = uv_venv(venv_path)
        if not ok:
            return False, f"could not create venv: {out}"
    return _run([uv, "pip", "install", "--python", str(vpy)] + packages,
                timeout=600)


def sudo_ready() -> bool:
    """True if sudo works without a password prompt."""
    if not shutil.which("sudo"):
        return False
    ok, _ = _run(["sudo", "-n", "true"], timeout=15)
    return ok


def apt_install(packages: list[str]) -> tuple[bool, str]:
    err = validate_apt(packages)
    if err:
        return False, err
    if not shutil.which("apt-get"):
        return False, "apt-get not found — not a Debian-based system"
    if not sudo_ready():
        return False, ("sudo needs a password (or sudo is not configured). "
                       "Aidon never handles passwords: configure "
                       "passwordless sudo for apt, or run the install "
                       "yourself.")
    return _run(["sudo", "-n", "apt-get", "install", "-y"] + packages,
                timeout=600)
