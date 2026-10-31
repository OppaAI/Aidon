"""Execution backends: Docker now, sandbox kept, VM over SSH.

Docker command construction is tested with a fake docker
path, not by running Docker, which is not installed in
this VM. A Docker backend with no docker binary fails
closed, it does not fall back to a host subprocess. The
VM backend is tested with fake ssh/tar processes: no
real host is ever contacted, and the sudo password is
asserted to travel on stdin only, never in argv.
"""

import io
import subprocess
from pathlib import Path

import pytest

from src.capabilities.execution import (BubblewrapBackend, DockerBackend,
                           VMBackend, make_backend)
from src.capabilities.sandbox import Sandbox


def test_docker_command_is_locked_down(tmp_path):
    backend = DockerBackend(image="python:3.12-slim", docker_path="/usr/bin/docker")
    command = backend._docker_command(["python", "-c", "print(1)"], tmp_path)
    joined = " ".join(command)
    assert "--rm" in command
    assert "--cap-drop=ALL" in command
    assert "--network" in command and "none" in command
    assert "--user" in command and "1000:1000" in command
    assert "--privileged" not in command
    assert "docker.sock" not in joined
    assert str(tmp_path) in joined
    assert "/work" in joined
    assert command[-3:] == ["python", "-c", "print(1)"]


def test_docker_image_must_be_pinned():
    with pytest.raises(ValueError):
        DockerBackend(image="python:latest")
    with pytest.raises(ValueError):
        DockerBackend(image="python")


def test_docker_missing_fails_closed(tmp_path):
    backend = DockerBackend(image="python:3.12-slim", docker_path=None)
    # Force missing, even if a test machine has docker.
    backend.docker_path = None
    with pytest.raises(RuntimeError):
        backend.run(["echo", "hi"], tmp_path)


def test_make_backend_owner_chosen_not_model(tmp_path):
    sandbox = Sandbox(tmp_path / "work")
    assert make_backend("bubblewrap", sandbox).name == "bubblewrap"
    assert make_backend("docker", sandbox).name == "docker"
    assert make_backend("vm", sandbox, vm_host="vmhost").name == "vm"
    with pytest.raises(ValueError):
        make_backend("vm", sandbox)


class _FakeProc:
    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = io.BytesIO(b"")

    def wait(self):
        return 0


class _FakeRun:
    def __init__(self, calls, timeout_on_exec=False):
        self.calls = calls
        self.timeout_on_exec = timeout_on_exec

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.timeout_on_exec and any(
                isinstance(a, str) and a.startswith("cd ") for a in args):
            raise subprocess.TimeoutExpired(args, 120)
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")


def _vm_backend(monkeypatch, calls, **kwargs):
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(subprocess, "run", _FakeRun(calls))
    kwargs.setdefault("ssh_path", "/usr/bin/ssh")
    return VMBackend(host="vmhost", **kwargs)


def test_vm_run_syncs_and_executes(tmp_path, monkeypatch):
    calls = []
    backend = _vm_backend(monkeypatch, calls)
    result = backend.run(["echo", "hi"], tmp_path / "work")
    assert result.ok and result.isolated and result.backend == "vm"
    ssh_calls = [args for args, _ in calls if args[0] == "/usr/bin/ssh"]
    assert len(ssh_calls) == 2  # sync up, execute (sync back is ssh Popen)
    assert any("mkdir -p" in a for args in ssh_calls for a in args)
    assert any(a.startswith("cd ") and "echo" in a
               for args in ssh_calls for a in args)
    assert any("filter='data'" in str(args) for args, _ in calls)


def test_vm_sudo_password_via_stdin_only(tmp_path, monkeypatch):
    calls = []
    backend = _vm_backend(monkeypatch, calls, use_sudo=True,
                          sudo_password="s3cret")
    backend.run(["echo", "hi"], tmp_path / "work")
    exec_call = [kw for args, kw in calls
                 if any(isinstance(a, str) and a.startswith("sudo -S")
                        for a in args)][0]
    assert exec_call.get("input") == "s3cret\n"
    for args, _ in calls:
        assert "s3cret" not in " ".join(str(a) for a in args)


def test_vm_sudo_without_password_raises():
    with pytest.raises(ValueError):
        VMBackend(host="vmhost", use_sudo=True)


def test_vm_missing_ssh_fails_closed(tmp_path, monkeypatch):
    backend = VMBackend(host="vmhost", ssh_path="/usr/bin/ssh")
    backend.ssh_path = None
    with pytest.raises(RuntimeError):
        backend.run(["echo", "hi"], tmp_path / "work")


def test_vm_timeout_returns_124(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(subprocess, "run",
                        _FakeRun(calls, timeout_on_exec=True))
    backend = VMBackend(host="vmhost", ssh_path="/usr/bin/ssh")
    result = backend.run(["echo", "hi"], tmp_path / "work")
    assert result.returncode == 124


def test_bubblewrap_backend_wraps_sandbox(tmp_path, require_bubblewrap):
    sandbox = Sandbox(tmp_path / "work")
    backend = BubblewrapBackend(sandbox)
    result = backend.run(["echo", "hi"], tmp_path / "work")
    assert result.ok
    assert "hi" in result.stdout
    assert result.backend == "bubblewrap"
