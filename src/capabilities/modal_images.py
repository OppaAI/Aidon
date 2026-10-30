"""Capability image builder: two-phase Modal images for Aidon.

The owner's deploy script proved the shape: a networked
setup sandbox installs packages, the filesystem is
snapshotted, and a network-locked sandbox runs from the
snapshot. This module is Aidon's version of that flow,
with the safety properties the repo's rules require:

- Names are gated by SAFE_NAME (the script's regex) and
  packages by the script's approved lists, mirrored here
  with modal/sandbox_deploy.py as the source of truth.
  The approved list is the human's standing approval for
  those names; anything else needs a mandatory approval
  decision first, and a rejection aborts the build. This
  matches the privileged-install rule: installs never
  happen silently, even in autonomous mode.
- The result is verified by evidence, not assertion: a
  locked sandbox built from the snapshot must pass the
  test command (by default, pip show for each pip
  package and command -v for each apt package, which
  verifies installation without guessing import names)
  before anything is registered. A failed install or a
  failed test writes no manifest.
- The manifest (<home>/capability_images.json) is the
  durable artifact: name, package lists, base, build
  time, and the test that passed. Snapshot images are
  not assumed persistent across processes, so reuse
  rebuilds from the manifest (Modal caches image layers,
  so a rebuild of the same package list is cheap) and
  the snapshot image object serves the run that built
  it. build() returns both the record and the image.

What this module is not: it does not decide that a run
needs an image, and it does not wire a built image into
a run. The caller (a future loop integration, e.g. the
recovery router's acquire_capability route) proposes the
package list, approves it, builds, and hands the image
to a ModalBackend. The backend already accepts an image
object for exactly that handoff.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

# Mirrored from modal/sandbox_deploy.py; that file is the
# source of truth. If the lists change there, change them
# here in the same commit.
APPROVED_PIP = {"requests", "numpy", "pandas", "scipy",
                "matplotlib", "beautifulsoup4"}
APPROVED_APT = {"git", "curl", "ffmpeg", "tree", "jq"}
SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")

MANIFEST_FILENAME = "capability_images.json"


def _default_test_command(pip_packages, apt_packages) -> str:
    parts = []
    if pip_packages:
        parts.append("pip show " + " ".join(pip_packages))
    if apt_packages:
        parts.append("command -v " + " ".join(apt_packages))
    return " && ".join(parts)


class CapabilityImageBuilder:
    """Build, test, and register Modal capability images."""

    def __init__(self, home, app_name: str = "aidon-sandbox",
                 sandbox_factory=None, approve=None,
                 setup_timeout: int = 1200, test_timeout: int = 300,
                 governor=None):
        self.home = Path(home)
        self.app_name = app_name
        self._sandbox_factory = sandbox_factory
        self._approve = approve
        self.setup_timeout = setup_timeout
        self.test_timeout = test_timeout
        self._governor = governor

    def _base_image(self):
        import modal
        return (
            modal.Image.from_registry("ubuntu:24.04", add_python="3.12")
            .run_commands(
                "apt-get update",
                "DEBIAN_FRONTEND=noninteractive apt-get upgrade -y",
            )
            .apt_install("git", "curl", "build-essential", "tree")
            .run_commands("python -m pip install --upgrade pip")
            .pip_install("requests", "numpy")
        )

    def _create_sandbox(self, network: bool, image, timeout: int):
        if self._sandbox_factory is not None:
            return self._sandbox_factory(network, image)
        try:
            import modal
        except ImportError as exc:
            raise RuntimeError(
                "Capability image builds need the modal package: "
                "pip install modal, then run modal setup") from exc
        app = modal.App.lookup(self.app_name, create_if_missing=True)
        return modal.Sandbox.create(
            app=app, image=image, timeout=timeout,
            block_network=not network)

    @staticmethod
    def _exec(sb, cmd, timeout: int):
        proc = sb.exec(*cmd, timeout=timeout)
        out = proc.stdout.read()
        err = proc.stderr.read()
        proc.wait()
        return proc.returncode, out, err

    def _manifest_path(self) -> Path:
        return self.home / MANIFEST_FILENAME

    def _read_manifest(self) -> dict:
        path = self._manifest_path()
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text())
        except (ValueError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, name: str):
        return self._read_manifest().get(name)

    def list_images(self) -> list:
        return sorted(self._read_manifest().values(),
                      key=lambda r: r.get("name", ""))

    def build(self, name: str, pip_packages=(), apt_packages=(),
              test_command: str | None = None) -> dict:
        """Governed wrapper: one dependency-set count plus
        elapsed build time against the run's governor."""
        if self._governor is None:
            return self._build_inner(name, pip_packages, apt_packages,
                                     test_command)
        with self._governor.timed("dependency"):
            return self._build_inner(name, pip_packages, apt_packages,
                                     test_command)

    def _build_inner(self, name: str, pip_packages=(), apt_packages=(),
                     test_command: str | None = None) -> dict:
        """Build, test, and register one capability image.

        Returns {"record": manifest record, "image": snapshot
        image object}. Raises on any failure; a failed build
        writes no manifest entry.
        """
        pip_packages = sorted(set(pip_packages))
        apt_packages = sorted(set(apt_packages))
        resolved_pip: dict[str, str] = {}
        if not SAFE_NAME.match(name):
            raise ValueError(f"Invalid capability image name: {name!r}")
        if not pip_packages and not apt_packages:
            raise ValueError("Nothing to install: give pip or apt packages")
        for pkg in pip_packages + apt_packages:
            if not SAFE_NAME.match(pkg):
                raise ValueError(f"Invalid package name: {pkg!r}")
        non_approved = ((set(pip_packages) - APPROVED_PIP)
                        | (set(apt_packages) - APPROVED_APT))
        if non_approved:
            if self._approve is None:
                raise RuntimeError(
                    "Approval required for packages outside the approved "
                    f"list: {sorted(non_approved)}")
            decision = self._approve(
                "install", f"Build capability image {name}?",
                "Packages outside the approved list:\n"
                + "\n".join(sorted(non_approved)))
            verdict = getattr(decision, "verdict", None)
            if verdict not in ("approved", "auto_approved"):
                raise RuntimeError(
                    "Capability image build rejected by approval")

        setup = self._create_sandbox(True, self._base_image(),
                                     self.setup_timeout)
        try:
            if apt_packages:
                rc, _out, err = self._exec(
                    setup, ["apt-get", "update"], 300)
                if rc != 0:
                    raise RuntimeError(f"apt-get update failed: {err}")
                rc, _out, err = self._exec(
                    setup, ["env", "DEBIAN_FRONTEND=noninteractive",
                            "apt-get", "install", "-y", *apt_packages],
                    self.setup_timeout)
                if rc != 0:
                    raise RuntimeError(f"apt-get install failed: {err}")
            if pip_packages:
                rc, _out, err = self._exec(
                    setup, ["python", "-m", "pip", "install",
                            *pip_packages],
                    self.setup_timeout)
                if rc != 0:
                    raise RuntimeError(f"pip install failed: {err}")
                _frc, _freeze, _ferr = self._exec(
                    setup, ["python", "-m", "pip", "freeze"], 300)
                if _frc == 0:
                    for _line in _freeze.splitlines():
                        if "==" in _line:
                            _pname, _pver = _line.split("==", 1)
                            resolved_pip[_pname.strip().lower()] = _pver.strip()
            snapshot = setup.snapshot_filesystem()
        finally:
            try:
                setup.terminate()
            except Exception:
                pass

        test = self._create_sandbox(False, snapshot, self.test_timeout)
        try:
            script = test_command or _default_test_command(
                pip_packages, apt_packages)
            rc, _out, err = self._exec(
                test, ["sh", "-c", script], self.test_timeout)
            if rc != 0:
                raise RuntimeError(
                    f"capability image test failed: {err}")
        finally:
            try:
                test.terminate()
            except Exception:
                pass

        record = {
            "name": name,
            "pip": pip_packages,
            "apt": apt_packages,
            "resolved_pip": resolved_pip,
            "base": "ubuntu:24.04+py3.12",
            "built_at": datetime.now(timezone.utc).isoformat(),
            "test_command": script,
            "test_passed": True,
        }
        manifest = self._read_manifest()
        manifest[name] = record
        self.home.mkdir(parents=True, exist_ok=True)
        tmp_path = self._manifest_path().with_suffix(".tmp")
        tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
        os.replace(tmp_path, self._manifest_path())
        # A rebuild replaces the judged artifact: a health
        # verdict on the previous image must not survive.
        from .capability_hub import invalidate_health
        invalidate_health(self.home, f"image:{name}")
        return {"record": record, "image": snapshot}
