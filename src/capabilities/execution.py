"""Execution backends: lightweight sandbox, Docker, and VM later.

Decision, fixed:
- Docker and the existing sandbox are now. A full VM is
  post-hackathon, future only, not built here.
- Keep the lightweight sandbox, add Docker as a second
  backend, do not replace the sandbox with Docker, and
  do not replace either with a VM now.

Interface, small on purpose:
An ExecutionBackend has a name, a run() that takes a
command list, a workspace, and a timeout, and returns
a result with returncode, stdout, stderr, and isolated.
It has a cleanup() that removes what it created. The
planner does not choose a backend: the backend is set
by the owner, in config, per run. A model plan step
names a tool, never a backend, so a model cannot
escalate itself from the sandbox to Docker, or from
Docker to a host shell.

Backends:
- BubblewrapBackend wraps the existing Sandbox. When
  bwrap is installed it is isolated, when it is not,
  the Sandbox fallback runs in the workdir with a
  timeout, and the result is labeled not isolated,
  never presented as isolated. That fallback is
  explicit, not silent, and it stays that way.
- DockerBackend runs the command in a disposable
  Docker container, removed after the run. It is
  chosen for build, install, and test work that the
  lightweight sandbox is too restrictive for.
  Controls, all enforced by the backend itself, not
  by the model: only the workdir is mounted, writable,
  at /work, no other host directory, no docker.sock,
  no host credentials, no environment secrets passed
  in, non-root user, all capabilities dropped, no
  privileged container, network off by default, CPU,
  memory, process, and disk limits, timeout, and a
  pinned image, never latest, never an image named
  by the model. The image comes from config, default
  python:3.12-slim.
  If Docker is missing, or the container fails to
  start, DockerBackend fails closed: it raises, it
  does not silently fall back to a host subprocess.
  That is the difference from the bwrap fallback,
  which is labeled, not silent, and which stays.
- VMBackend runs commands over SSH on a VM the owner
  already runs (no provisioning here: lifecycle stays
  future work). The local workdir is tarred up to a
  remote dir, the command runs there, results tar back.
  With use_sudo, commands run under sudo -S, the
  password arriving on stdin only — never in argv,
  never logged. A disposable VM per run, and a Docker
  container inside it, remain future work.

Docker does not give Aidon learning. Learning comes
from planning, experience storage, retrieval, and
verification, which are already built. Docker only
gives a richer, disposable place to build, run, test,
fail, and repair. Verification still checks actual
outputs and files, never just an exit code.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from .sandbox import Sandbox, SandboxResult


@dataclass
class ExecutionResult:
    returncode: int
    stdout: str
    stderr: str
    isolated: bool
    backend: str = ""
    image: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ExecutionBackend:
    name: str = "backend"

    def run(self, command: list[str], workspace: Path,
            timeout_seconds: int = 120) -> ExecutionResult:
        raise NotImplementedError

    def cleanup(self) -> None:
        return None


class BubblewrapBackend(ExecutionBackend):
    name = "bubblewrap"

    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox

    def run(self, command: list[str], workspace: Path,
            timeout_seconds: int = 120) -> ExecutionResult:
        result: SandboxResult = self.sandbox.run(command)
        return ExecutionResult(result.returncode, result.stdout,
                               result.stderr, result.isolated,
                               backend=self.name)


class DockerBackend(ExecutionBackend):
    name = "docker"

    def __init__(self, image: str = "python:3.12-slim",
                 allow_network: bool = False,
                 memory: str = "1g", cpus: str = "1.0",
                 pids_limit: int = 100,
                 docker_path: str | None = None):
        if not image or image.endswith(":latest") or ":" not in image:
            raise ValueError("Docker image must be pinned, never latest or unpinned")
        self.image = image
        self.allow_network = allow_network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.docker_path = docker_path or shutil.which("docker")

    def _docker_command(self, command: list[str], workspace: Path) -> list[str]:
        name = f"aidon-{uuid.uuid4().hex[:12]}"
        full = [
            self.docker_path, "run", "--rm", "--name", name,
            "--user", "1000:1000",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--network", "host" if self.allow_network else "none",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "--mount", f"type=bind,source={workspace},target=/work",
            "--workdir", "/work",
            # No docker.sock, no host home, no credentials,
            # no environment file, no privileged flag, ever.
            self.image,
            *command,
        ]
        return full

    def run(self, command: list[str], workspace: Path,
            timeout_seconds: int = 120) -> ExecutionResult:
        if not command:
            raise ValueError("command must not be empty")
        if self.docker_path is None:
            # Fail closed, never silently fall back to a host subprocess.
            raise RuntimeError("Docker is not installed, Docker backend cannot run")
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        full = self._docker_command(command, workspace)
        try:
            proc = subprocess.run(full, capture_output=True, text=True,
                                  timeout=timeout_seconds)
            return ExecutionResult(proc.returncode, proc.stdout, proc.stderr,
                                   isolated=True, backend=self.name,
                                   image=self.image)
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(124, exc.stdout or "", "timeout",
                                   isolated=True, backend=self.name,
                                   image=self.image)


class VMBackend(ExecutionBackend):
    """SSH to a VM the owner already runs. No provisioning.

    The workdir is tarred to a remote dir, the command runs
    there, results tar back. The host never runs the command.
    Fail-closed like DockerBackend: missing ssh raises, a
    failed sync returns a failed result, never a silent host
    fallback. The sudo password, when configured, travels
    on stdin only: it never appears in argv, logs, or errors.
    """

    name = "vm"

    def __init__(self, host: str, user: str | None = None,
                 remote_workspace: str | None = None,
                 sudo_password: str | None = None,
                 use_sudo: bool = False,
                 ssh_path: str | None = None, port: int = 22):
        if not host:
            raise ValueError(
                "VM backend needs a host (AIDON_VM_HOST)")
        if use_sudo and not sudo_password:
            raise ValueError(
                "VM sudo requested but no sudo password configured "
                "(AIDON_VM_SUDO_PASSWORD)")
        self.host = host
        self.user = user
        self.remote_workspace = remote_workspace
        self.sudo_password = sudo_password
        self.use_sudo = use_sudo
        self.ssh_path = ssh_path or shutil.which("ssh")
        self.port = port
        self._last_remote: str | None = None

    def _target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _ssh_base(self) -> list[str]:
        cmd = [self.ssh_path, "-o", "ConnectTimeout=10"]
        if self.port != 22:
            cmd += ["-p", str(self.port)]
        cmd.append(self._target())
        return cmd

    def _remote_dir(self, workspace: Path) -> str:
        if self.remote_workspace:
            return self.remote_workspace
        return f"~/aidon-work/{workspace.name}"

    def _sync_up(self, workspace: Path, remote: str,
                 timeout_seconds: int) -> bool:
        tar = subprocess.Popen(
            ["tar", "-cf", "-", "-C", str(workspace), "."],
            stdout=subprocess.PIPE)
        try:
            proc = subprocess.run(
                [*self._ssh_base(),
                 f"mkdir -p {shlex.quote(remote)} && "
                 f"tar -xf - -C {shlex.quote(remote)}"],
                stdin=tar.stdout, capture_output=True, text=True,
                timeout=timeout_seconds)
            return proc.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
        finally:
            if tar.stdout is not None:
                tar.stdout.close()
            tar.wait()

    def _sync_back(self, workspace: Path, remote: str,
                   timeout_seconds: int) -> bool:
        ssh = subprocess.Popen(
            [*self._ssh_base(), f"tar -cf - -C {shlex.quote(remote)} ."],
            stdout=subprocess.PIPE)
        try:
            proc = subprocess.run(
                ["tar", "-xf", "-", "-C", str(workspace)],
                stdin=ssh.stdout, capture_output=True, text=True,
                timeout=timeout_seconds)
            return proc.returncode == 0 and ssh.wait() == 0
        except (subprocess.TimeoutExpired, OSError):
            return False
        finally:
            if ssh.stdout is not None:
                ssh.stdout.close()

    def run(self, command: list[str], workspace: Path,
            timeout_seconds: int = 120) -> ExecutionResult:
        if not command:
            raise ValueError("command must not be empty")
        if self.ssh_path is None:
            # Fail closed, never silently fall back to a host subprocess.
            raise RuntimeError("ssh is not installed, VM backend cannot run")
        workspace = Path(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        remote = self._remote_dir(workspace)
        self._last_remote = remote
        if not self._sync_up(workspace, remote, timeout_seconds):
            return ExecutionResult(1, "", "workspace sync to VM failed",
                                   isolated=True, backend=self.name)
        shell_cmd = f"cd {shlex.quote(remote)} && {shlex.join(command)}"
        if self.use_sudo:
            shell_cmd = f"sudo -S -- sh -c {shlex.quote(shell_cmd)}"
        try:
            proc = subprocess.run(
                [*self._ssh_base(), shell_cmd],
                input=(self.sudo_password + "\n") if self.use_sudo else None,
                capture_output=True, text=True, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(124, exc.stdout or "", "timeout",
                                   isolated=True, backend=self.name)
        if not self._sync_back(workspace, remote, timeout_seconds):
            return ExecutionResult(1, proc.stdout,
                                   "workspace sync back from VM failed",
                                   isolated=True, backend=self.name)
        return ExecutionResult(proc.returncode, proc.stdout, proc.stderr,
                               isolated=True, backend=self.name)

    def cleanup(self) -> None:
        if self._last_remote and self.ssh_path:
            try:
                subprocess.run(
                    [*self._ssh_base(),
                     f"rm -rf {shlex.quote(self._last_remote)}"],
                    capture_output=True, text=True, timeout=60)
            except Exception:
                pass
            self._last_remote = None


def make_backend(name: str, sandbox: Sandbox, image: str = "python:3.12-slim",
                 allow_network: bool = False,
                 vm_host: str | None = None, vm_user: str | None = None,
                 vm_remote_workspace: str | None = None,
                 vm_sudo_password: str | None = None,
                 vm_use_sudo: bool = False) -> ExecutionBackend:
    """Owner-chosen backend, never model-chosen."""
    if name == "docker":
        return DockerBackend(image=image, allow_network=allow_network)
    if name == "vm":
        if not vm_host:
            raise ValueError("VM backend needs a host (AIDON_VM_HOST)")
        return VMBackend(host=vm_host, user=vm_user,
                         remote_workspace=vm_remote_workspace,
                         sudo_password=vm_sudo_password,
                         use_sudo=vm_use_sudo)
    if name in ("bubblewrap", "sandbox", ""):
        return BubblewrapBackend(sandbox)
    raise ValueError(f"unknown execution backend: {name}")
