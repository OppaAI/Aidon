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

Only the isolated backend restricts filesystem access. The optional
subprocess fallback is not a security boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import signal
import threading
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
                 require_isolation: bool = True):
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

    def run(self, command: list[str], env: dict | None = None,
            input_text: str | None = None) -> SandboxResult:
        """Run command inside the workdir. command is a list, never a shell string.

        ``env`` adds named variables to the child environment. The
        bubblewrap backend refuses it because bwrap's ``--setenv``
        would expose values in the host command line. Send secrets in
        ``input_text`` and let the isolated runner read them from stdin.
        """
        if not command:
            raise ValueError("command must not be empty")
        if self.require_isolation and not self.bwrap:
            return SandboxResult(
                126, "", "REFUSED: isolation is required but bubblewrap "
                "is unavailable or disabled", False)
        if self.bwrap:
            if env:
                raise ValueError(
                    "env values are not accepted by bubblewrap; pass data "
                    "through stdin instead")
            # Merge fix: run() used to inline a simpler argv here while
            # _bwrap_argv (the merged-usr version, with /bin, /sbin,
            # /lib, /lib64 symlinks, /proc, and the interpreter tree)
            # sat unused below it. The inline version is gone; the
            # one builder is the live path, so the two cannot drift.
            full = self._bwrap_argv(command, env=env)
            isolated = True
        else:
            full = list(command)
            isolated = False
        run_env = None
        if env:
            run_env = dict(os.environ)
            run_env.update({str(k): str(v) for k, v in env.items()})
        proc = subprocess.Popen(
            full, cwd=self.workdir, stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=run_env,
            start_new_session=True, preexec_fn=self._apply_resource_limits)
        limit = 1024 * 1024
        outputs = [bytearray(), bytearray()]
        truncated = [False, False]
        def drain(stream, index):
            try:
                while chunk := stream.read(65536):
                    remaining = limit - len(outputs[index])
                    outputs[index].extend(chunk[:max(0, remaining)])
                    if len(chunk) > remaining:
                        truncated[index] = True
            finally:
                stream.close()
        readers = [threading.Thread(target=drain, args=(stream, i), daemon=True)
                   for i, stream in enumerate((proc.stdout, proc.stderr))]
        for reader in readers:
            reader.start()
        if input_text is not None:
            def send_input():
                try:
                    proc.stdin.write(input_text.encode())
                    proc.stdin.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=send_input, daemon=True).start()
        timed_out = False
        try:
            proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
        # Terminate descendants even if the leader exited while they held pipes.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        for reader in readers:
            reader.join(timeout=2)
        values = [bytes(value).decode("utf-8", errors="replace") +
                  ("\n[output truncated]" if truncated[i] else "")
                  for i, value in enumerate(outputs)]
        return SandboxResult(124 if timed_out else proc.returncode, values[0],
                             "timeout" if timed_out else values[1], isolated)

    def _apply_resource_limits(self) -> None:
        """Apply Aidon's optional POSIX limits to the child process.

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

    def _bwrap_argv(self, command: list[str],
                    env: dict | None = None) -> list[str]:
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
            # Start from an empty root. Binding only selected system
            # paths prevents the sandbox from inheriting the host's
            # /home, /tmp, and other writable locations.
            "--tmpfs", "/",
            "--setenv", "PATH", f"{sys.prefix}/bin:/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", wd,
            "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8",
            "--setenv", "TERM", "dumb",
            "--ro-bind-try", "/usr", "/usr",
        ]
        for key, value in (env or {}).items():
            argv += ["--setenv", str(key), str(value)]
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
        # Temporary files belong to this run and never share the host's
        # /tmp. The workdir is mounted separately below.
        argv += ["--dir", "/tmp", "--tmpfs", "/tmp"]
        # The interpreter may live outside /usr (a uv-managed venv
        # points into ~/.local/share/uv/python/...), so bind its tree
        # read-only, or `python3` inside the sandbox is a dangling
        # symlink and every generated script fails to start.
        for extra in self._interpreter_exposes():
            argv += ["--ro-bind-try", extra, extra]
        workdir = Path(wd)
        protected = (Path("/usr"), Path("/bin"), Path("/sbin"),
                     Path("/lib"), Path("/lib64"), Path("/etc"),
                     Path("/proc"), Path("/dev"))
        if any(workdir == root or root in workdir.parents
               for root in protected):
            raise ValueError(
                "sandbox workdir cannot be inside a system mount")
        # Recreate only the workdir's parent names in the fresh root and
        # make them read-only. Otherwise a workdir under /tmp or /home
        # would leave its host siblings writable inside the sandbox.
        parents = [parent for parent in workdir.parents
                   if parent not in (Path("/"), Path("/tmp"))]
        for parent in reversed(parents):
            argv += ["--dir", str(parent)]
        argv += ["--dir", wd]
        argv += ["--bind", wd, wd]
        for parent in parents:
            argv += ["--chmod", "0555", str(parent)]
        argv += ["--chdir", wd, "--", *command]
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
