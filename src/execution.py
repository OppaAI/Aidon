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
- VMBackend is post-hackathon, future only. It is
  documented here, not implemented, and there is no
  stub that pretends to run a VM. When it is built,
  it will be a disposable VM running a Docker
  container inside it, for untrusted code and for
  work that needs a separate kernel.

Docker does not give Aidon learning. Learning comes
from planning, experience storage, retrieval, and
verification, which are already built. Docker only
gives a richer, disposable place to build, run, test,
fail, and repair. Verification still checks actual
outputs and files, never just an exit code.
"""

from __future__ import annotations

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


def make_backend(name: str, sandbox: Sandbox, image: str = "python:3.12-slim",
                 allow_network: bool = False) -> ExecutionBackend:
    """Owner-chosen backend, never model-chosen."""
    if name == "docker":
        return DockerBackend(image=image, allow_network=allow_network)
    if name in ("bubblewrap", "sandbox", ""):
        return BubblewrapBackend(sandbox)
    raise ValueError(f"unknown execution backend: {name}")
