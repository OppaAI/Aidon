"""Modal worker pool: branch isolation, concurrent runs,
strict merge, cleanup, and the worker cap."""

import subprocess

import pytest

from src.capabilities.modal_workers import ModalWorkerPool


class _StubBackend:
    """Runs commands locally in the worker workspace, like a
    sandbox would, and records cleanup."""

    def __init__(self):
        self.cleaned = False
        self.runs = []

    def run(self, command, workspace, timeout_seconds=120):
        from pathlib import Path
        workspace = Path(workspace)
        self.runs.append((command, workspace))
        proc = subprocess.run(command, shell=True, cwd=str(workspace),
                              capture_output=True, text=True)
        return SimpleNamespaceShim(proc.returncode, proc.stdout,
                                   proc.stderr)

    def cleanup(self):
        self.cleaned = True


class SimpleNamespaceShim:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _pool(tmp_path, max_workers=4):
    backends = {}

    def _factory(name):
        backend = _StubBackend()
        backends[name] = backend
        return backend

    pool = ModalWorkerPool(tmp_path / "pool", max_workers=max_workers,
                           backend_factory=_factory)
    pool._backends_for_test = backends
    return pool


def test_workers_are_isolated_from_each_other(tmp_path):
    pool = _pool(tmp_path)
    ws_a = pool.spawn("a")
    ws_b = pool.spawn("b")
    results = pool.run_all([
        ("a", "echo hello-a > out.txt"),
        ("b", "echo hello-b > out.txt"),
    ])
    assert results["a"].returncode == 0
    assert results["b"].returncode == 0
    assert (ws_a / "out.txt").read_text().strip() == "hello-a"
    assert (ws_b / "out.txt").read_text().strip() == "hello-b"
    assert not (ws_a / "other.txt").exists()


def test_merge_combines_disjoint_outputs(tmp_path):
    pool = _pool(tmp_path)
    ws_a = pool.spawn("a")
    ws_b = pool.spawn("b")
    pool.run_all([
        ("a", "mkdir -p sub && echo A > sub/a.txt"),
        ("b", "echo B > b.txt"),
    ])
    target = tmp_path / "integration"
    merged = pool.merge_into([ws_a, ws_b], target)
    assert (target / "sub" / "a.txt").read_text().strip() == "A"
    assert (target / "b.txt").read_text().strip() == "B"
    assert merged == ["b.txt", "sub/a.txt"]


def test_merge_collision_raises_before_copying(tmp_path):
    pool = _pool(tmp_path)
    ws_a = pool.spawn("a")
    ws_b = pool.spawn("b")
    pool.run_all([
        ("a", "echo A > shared.txt"),
        ("b", "echo B > shared.txt"),
    ])
    target = tmp_path / "integration"
    with pytest.raises(ValueError, match="shared.txt"):
        pool.merge_into([ws_a, ws_b], target)
    assert not (target / "shared.txt").exists(), \
        "a failed merge must not leave a half-merged directory"


def test_worker_failure_stays_in_its_result(tmp_path):
    pool = _pool(tmp_path)
    pool.spawn("good")
    pool.spawn("bad")
    results = pool.run_all([
        ("good", "echo ok > ok.txt"),
        ("bad", "exit 3"),
    ])
    assert results["good"].returncode == 0
    assert results["bad"].returncode == 3


def test_unknown_worker_and_duplicate_and_cap(tmp_path):
    pool = _pool(tmp_path, max_workers=2)
    pool.spawn("a")
    with pytest.raises(ValueError, match="unknown worker"):
        pool.run("ghost", "echo hi")
    with pytest.raises(ValueError, match="already exists"):
        pool.spawn("a")
    pool.spawn("b")
    with pytest.raises(RuntimeError, match="worker cap"):
        pool.spawn("c")


def test_cleanup_terminates_all_workers(tmp_path):
    pool = _pool(tmp_path)
    pool.spawn("a")
    pool.spawn("b")
    pool.cleanup()
    assert all(b.cleaned for b in pool._backends_for_test.values())
    with pytest.raises(ValueError, match="unknown worker"):
        pool.run("a", "echo hi")


def test_context_manager_cleans_up(tmp_path):
    with _pool(tmp_path) as pool:
        pool.spawn("a")
        backends = pool._backends_for_test
    assert backends["a"].cleaned is True
