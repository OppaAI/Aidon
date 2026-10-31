"""Capability image builder: two-phase build, approval gate,
test-before-register, and manifest persistence, all against
a fake Modal sandbox."""

import json
from types import SimpleNamespace

import pytest

from src.capabilities.modal_images import CapabilityImageBuilder


class _Stream:
    def __init__(self, data=""):
        self._data = data

    def read(self):
        return self._data


class _Proc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self.returncode = returncode

    def wait(self):
        return self.returncode


class _FakeSandbox:
    def __init__(self, fail_install=False, fail_test=False):
        self.exec_calls = []
        self.fail_install = fail_install
        self.fail_test = fail_test
        self.snapshot_called = False
        self.terminated = False

    def exec(self, *cmd, timeout=None):
        self.exec_calls.append(cmd)
        joined = " ".join(cmd)
        if "pip" in cmd and "install" in cmd:
            if self.fail_install:
                return _Proc(returncode=1, stderr="pip boom")
            return _Proc(returncode=0)
        if "apt-get" in cmd:
            if self.fail_install:
                return _Proc(returncode=1, stderr="apt boom")
            return _Proc(returncode=0)
        if cmd[:2] == ("sh", "-c"):
            if self.fail_test:
                return _Proc(returncode=1, stderr="test boom")
            return _Proc(returncode=0)
        return _Proc(returncode=0)

    def snapshot_filesystem(self):
        self.snapshot_called = True
        return "snapshot-image"

    def terminate(self):
        self.terminated = True


class _Factory:
    def __init__(self, fail_install=False, fail_test=False):
        self.created = []
        self.fail_install = fail_install
        self.fail_test = fail_test

    def __call__(self, network, image):
        sb = _FakeSandbox(fail_install=self.fail_install,
                          fail_test=self.fail_test)
        self.created.append({"network": network, "image": image,
                             "sandbox": sb})
        return sb


def _builder(tmp_path, factory=None, approve=None):
    builder = CapabilityImageBuilder(
        tmp_path, sandbox_factory=factory, approve=approve)
    # The real base image needs the modal package; tests inject
    # a sentinel instead.
    builder._base_image = lambda: "base-image"
    return builder


def test_happy_path_builds_tests_registers(tmp_path):
    factory = _Factory()
    builder = _builder(tmp_path, factory=factory)
    result = builder.build("scipy-env", pip_packages=["scipy"],
                           apt_packages=["jq"])
    assert result["image"] == "snapshot-image"
    record = result["record"]
    assert record["name"] == "scipy-env"
    assert record["pip"] == ["scipy"]
    assert record["apt"] == ["jq"]
    assert record["test_passed"] is True
    # Setup sandboxed with network, test locked from the snapshot.
    assert factory.created[0]["network"] is True
    assert factory.created[0]["image"] == "base-image"
    assert factory.created[1]["network"] is False
    assert factory.created[1]["image"] == "snapshot-image"
    # Default test verifies installation, not import names.
    test_calls = factory.created[1]["sandbox"].exec_calls
    assert any("pip show scipy" in " ".join(c) for c in test_calls)
    assert any("command -v jq" in " ".join(c) for c in test_calls)
    # Manifest persisted and readable by a fresh builder.
    fresh = _builder(tmp_path)
    assert fresh.get("scipy-env")["pip"] == ["scipy"]
    assert [r["name"] for r in fresh.list_images()] == ["scipy-env"]


def test_approved_packages_skip_approval(tmp_path):
    def _never_approve(kind, title, body):
        raise AssertionError("approved packages must not ask")

    factory = _Factory()
    builder = _builder(tmp_path, factory=factory, approve=_never_approve)
    builder.build("numpy-env", pip_packages=["numpy"])
    assert factory.created, "approved build must proceed"


def test_non_approved_without_approve_raises_before_sandbox(tmp_path):
    factory = _Factory()
    builder = _builder(tmp_path, factory=factory, approve=None)
    with pytest.raises(RuntimeError, match="Approval required"):
        builder.build("cowsay-env", pip_packages=["cowsay"])
    assert factory.created == [], "no sandbox before approval"


def test_non_approved_rejected_by_approval(tmp_path):
    factory = _Factory()
    builder = _builder(
        tmp_path, factory=factory,
        approve=lambda kind, title, body: SimpleNamespace(
            verdict="rejected"))
    with pytest.raises(RuntimeError, match="rejected"):
        builder.build("cowsay-env", pip_packages=["cowsay"])
    assert factory.created == []
    assert builder.get("cowsay-env") is None


def test_non_approved_approved_builds(tmp_path):
    factory = _Factory()
    builder = _builder(
        tmp_path, factory=factory,
        approve=lambda kind, title, body: SimpleNamespace(
            verdict="approved"))
    result = builder.build("cowsay-env", pip_packages=["cowsay"])
    assert result["record"]["pip"] == ["cowsay"]


def test_invalid_names_rejected(tmp_path):
    builder = _builder(tmp_path, factory=_Factory())
    with pytest.raises(ValueError):
        builder.build("Bad Name!", pip_packages=["numpy"])
    with pytest.raises(ValueError):
        builder.build("ok-name", pip_packages=["Bad_Package"])
    with pytest.raises(ValueError):
        builder.build("ok-name", pip_packages=["numpy; rm -rf /"])


def test_empty_package_list_rejected(tmp_path):
    builder = _builder(tmp_path, factory=_Factory())
    with pytest.raises(ValueError, match="Nothing to install"):
        builder.build("empty-env")


def test_install_failure_writes_no_manifest(tmp_path):
    factory = _Factory(fail_install=True)
    builder = _builder(tmp_path, factory=factory)
    with pytest.raises(RuntimeError, match="pip install failed"):
        builder.build("numpy-env", pip_packages=["numpy"])
    assert builder.get("numpy-env") is None
    assert factory.created[0]["sandbox"].terminated is True
    assert factory.created[0]["sandbox"].snapshot_called is False


def test_test_failure_writes_no_manifest(tmp_path):
    factory = _Factory(fail_test=True)
    builder = _builder(tmp_path, factory=factory)
    with pytest.raises(RuntimeError, match="test failed"):
        builder.build("numpy-env", pip_packages=["numpy"])
    assert builder.get("numpy-env") is None
    assert len(factory.created) == 2, "snapshot built, then test failed"
    assert factory.created[1]["sandbox"].terminated is True


def test_custom_test_command_used(tmp_path):
    factory = _Factory()
    builder = _builder(tmp_path, factory=factory)
    builder.build("numpy-env", pip_packages=["numpy"],
                  test_command="python -c 'import numpy'")
    test_calls = factory.created[1]["sandbox"].exec_calls
    assert any("import numpy" in " ".join(c) for c in test_calls)
