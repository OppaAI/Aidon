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
            full = [
                self.bwrap,
                "--unshare-net", "--unshare-pid",
                "--ro-bind", "/usr", "/usr",
                "--ro-bind", "/lib", "/lib",
                "--ro-bind", "/etc", "/etc",
                "--dev", "/dev",
                "--bind", str(self.workdir), str(self.workdir),
                "--chdir", str(self.workdir),
                "--clearenv",
                "--setenv", "PATH", "/usr/bin:/bin",
                "--",
                *command,
            ]
            # On Ubuntu, /lib64 is a symlink to usr/lib64, and it holds
            # the dynamic linker /lib64/ld-linux-x86-64.so.2. Without
            # binding it, bwrap fails with "execvp echo: No such file
            # or directory", even though /usr/bin/echo exists, because
            # the linker itself is missing inside the sandbox. Bind it
            # only if it exists, so non-Ubuntu systems do not fail on
            # a missing source path. Insert before the "--" separator.
            if Path("/lib64").exists() or Path("/lib64").is_symlink():
                sep = full.index("--")
                full[sep:sep] = ["--ro-bind", "/lib64", "/lib64"]
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
