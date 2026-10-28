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
    def __init__(self, workdir: Path, timeout_seconds: int = 120,
                 mem_mb: int = 1024, nproc: int = 256,
                 fsize_mb: int = 512, cpu_seconds: int = 0,
                 backend: str = "auto", allow_network: bool = False,
                 require_isolation: bool = False):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        self.mem_mb = max(0, int(mem_mb))
        self.nproc = max(0, int(nproc))
        self.fsize_mb = max(0, int(fsize_mb))
        self.cpu_seconds = max(0, int(cpu_seconds))
        self.backend = str(backend or "auto").lower()
        self.allow_network = bool(allow_network)
        self.require_isolation = bool(require_isolation)
        self.bwrap = (shutil.which("bwrap")
                      if self.backend in ("auto", "bubblewrap") else None)

    @property
    def isolated(self) -> bool:
        return self.bwrap is not None

    def run(self, command: list[str]) -> SandboxResult:
        """Run command inside the workdir. command is a list, never a shell string."""
        if not command:
            raise ValueError("command must not be empty")
        if self.require_isolation and not self.bwrap:
            return SandboxResult(
                126, "", "REFUSED: isolation is required but bubblewrap "
                "is unavailable or disabled", False)
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
                timeout=self.timeout_seconds,
                preexec_fn=self._apply_resource_limits)
            return SandboxResult(proc.returncode, proc.stdout, proc.stderr,
                                 isolated)
        except subprocess.TimeoutExpired as exc:
            return SandboxResult(124, exc.stdout or "", "timeout", isolated)

    def _apply_resource_limits(self) -> None:
        """Apply old Aidon's optional POSIX limits to the child process.

        RLIMIT_NPROC is deliberately not set here. Linux counts every
        task (thread) of the uid against it, not the child's own
        children, so a 256 cap is already exceeded on an ordinary
        desktop (a browser alone has hundreds of threads). The fork
        inside bwrap then fails with EAGAIN, which surfaces as
        "Creating new namespace failed: Resource temporarily
        unavailable" and makes every sandboxed step fail on a busy
        machine. Process isolation comes from bwrap's --unshare-pid
        and the wall-clock timeout, not from a per-uid task cap.
        ``nproc`` is kept as a constructor argument so existing
        callers and config keys keep working.
        """
        try:
            import resource
            limits = (
                (resource.RLIMIT_AS, self.mem_mb * 1024 * 1024),
                (resource.RLIMIT_FSIZE, self.fsize_mb * 1024 * 1024),
                (resource.RLIMIT_CPU, self.cpu_seconds),
            )
            for kind, value in limits:
                if value <= 0:
                    continue
                current = resource.getrlimit(kind)
                hard = current[1]
                if hard != resource.RLIM_INFINITY:
                    value = min(value, hard)
                resource.setrlimit(kind, (value, value))
        except (ImportError, OSError, ValueError):
            # Resource limits are defense-in-depth; the wall-clock timeout
            # and isolation boundary remain active if a platform refuses one.
            return

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
            "--unshare-pid", "--die-with-parent",
            "--clearenv",
            "--setenv", "PATH", f"{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", wd,
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "TERM", "dumb",
            "--ro-bind-try", "/usr", "/usr",
        ]
        if not self.allow_network:
            argv.insert(1, "--unshare-net")
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
