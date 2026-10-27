"""Sandbox: run a command in an isolated workdir, or say so loudly.

The sandbox is where Aidon tests a plan before it touches anything
real. A command runs only inside a workdir under the Aidon home,
with a timeout, and with network off when the backend supports it.

Backends, in order of strength:
- bubblewrap (bwrap), when it is installed: unshared network and
  PID space, read-only system directories, a masked home, and a
  cleared environment. This is the only backend that counts as
  isolated.
- subprocess fallback, when bwrap is missing: the command still
  runs in the workdir with a timeout, but it is NOT isolated, and
  every result from it is labeled as such. Never present a fallback
  run as an isolated run.

A sandbox run never writes outside its workdir. The loop checks
that by running in the workdir and by never passing a path outside
it to the command.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    isolated: bool

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Sandbox:
    def __init__(self, workdir: Path, timeout_seconds: int = 120):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.bwrap = shutil.which("bwrap")

    @property
    def isolated(self) -> bool:
        return self.bwrap is not None

    def run(self, command: list[str]) -> SandboxResult:
        """Run command inside the workdir. command is a list, never a shell string."""
        if not command:
            raise ValueError("command must not be empty")
        if self.bwrap:
            # Merge fix: run() used to inline a simpler argv here while
            # _bwrap_argv (the merged-usr version, with /bin, /sbin,
            # /lib, /lib64 symlinks, /proc, and the interpreter tree)
            # sat unused below it. The inline version is gone; the
            # one builder is the live path, so the two cannot drift.
            full = self._bwrap_argv(command)
            isolated = True
        else:
            full = list(command)
            isolated = False
        try:
            proc = subprocess.run(
                full, cwd=self.workdir, capture_output=True, text=True,
                timeout=self.timeout_seconds)
            return SandboxResult(proc.returncode, proc.stdout, proc.stderr,
                                 isolated)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(124, exc.stdout or "", "timeout", isolated)

    def _bwrap_argv(self, command: list[str]) -> list[str]:
        """Build the bubblewrap invocation for one command.

        Mounts are applied in order. The system directories are
        recreated exactly as the host has them: on a merged-usr
        distro ``/bin``, ``/sbin``, ``/lib`` and ``/lib64`` are
        symlinks into ``/usr``. Binding only ``/usr`` leaves the
        ELF interpreter (``/lib64/ld-linux-*.so``) missing, and
        then *every* command dies with ``execvp: No such file or
        directory`` even though the binary is present.
        """
        import os
        import sys
        from pathlib import Path

        wd = str(self.workdir.resolve())
        argv = [
            self.bwrap,
            "--unshare-net", "--unshare-pid", "--die-with-parent",
            "--clearenv",
            "--setenv", "PATH", f"{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", wd,
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "TERM", "dumb",
            "--ro-bind-try", "/usr", "/usr",
        ]
        # Recreate symlinked system dirs as symlinks (merged-usr),
        # and bind real ones read-only.
        for sysdir in ("/bin", "/sbin", "/lib", "/lib64", "/etc"):
            path = Path(sysdir)
            if path.is_symlink():
                argv += ["--symlink", os.readlink(path), sysdir]
            else:
                argv += ["--ro-bind-try", sysdir, sysdir]
        argv += ["--proc", "/proc", "--dev", "/dev"]
        # The interpreter may live outside /usr (a uv-managed venv
        # points into ~/.local/share/uv/python/...), so bind its tree
        # read-only, or `python3` inside the sandbox is a dangling
        # symlink and every generated script fails to start.
        for extra in self._interpreter_exposes():
            argv += ["--ro-bind-try", extra, extra]
        argv += ["--bind", wd, wd, "--chdir", wd, "--", *command]
        return argv

    @staticmethod
    def _interpreter_exposes() -> list[str]:
        """Directories the running interpreter needs, outside /usr."""
        import sys
        from pathlib import Path

        seen: list[str] = []
        candidates = [Path(sys.prefix), Path(sys.executable).resolve().parent]
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            if not resolved.is_dir():
                continue
            if str(resolved) in ("/", "/usr", "/usr/local"):
                continue
            if str(resolved) not in seen:
                seen.append(str(resolved))
        return seen
