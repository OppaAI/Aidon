"""Execution backends: Docker now, sandbox kept, VM future only.

Docker command construction is tested with a fake docker
path, not by running Docker, which is not installed in
this VM. A Docker backend with no docker binary fails
closed, it does not fall back to a host subprocess.
"""

from pathlib import Path

import pytest

from src.execution import (BubblewrapBackend, DockerBackend,
                           make_backend)
from src.sandbox import Sandbox


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
    with pytest.raises(ValueError):
        make_backend("vm", sandbox)


def test_bubblewrap_backend_wraps_sandbox(tmp_path):
    sandbox = Sandbox(tmp_path / "work")
    backend = BubblewrapBackend(sandbox)
    result = backend.run(["echo", "hi"], tmp_path / "work")
    assert result.ok
    assert "hi" in result.stdout
    assert result.backend == "bubblewrap"
