"""Gap 4: skill trust requires pinned dependencies.
Gap 4: builder records resolved pip versions.
Gap 4: Modal backend carries CPU/memory limits."""

import sys
from types import SimpleNamespace

import pytest

from src.capabilities.execution import ModalBackend, make_backend
from src.capabilities.modal_images import CapabilityImageBuilder
from src.capabilities.skill_library import Skill, SkillLibrary


class _Sandbox:
    def run(self, command):
        return {"ok": True, "output": "pass"}


def test_trust_requires_pinned_dependencies(tmp_path):
    lib = SkillLibrary(tmp_path)
    lib.add(Skill(name="loose", description="x",
                  dependencies=["requests"],
                  test_commands=["python3 --version"]))
    report = lib.run_tests("loose", _Sandbox())
    lib.promote("loose", report)
    with pytest.raises(ValueError, match="pinned"):
        lib.trust("loose", approved=True)
    assert lib.get("loose").state == "validated"


def test_trust_accepts_pinned_and_dict_dependencies(tmp_path):
    lib = SkillLibrary(tmp_path)
    lib.add(Skill(name="pinned", description="x",
                  dependencies=["requests==2.32.3"],
                  test_commands=["python3 --version"]))
    lib.add(Skill(name="verified", description="x",
                  dependencies=[{"name": "numpy", "version": "1.26.0"}],
                  test_commands=["python3 --version"]))
    for name in ("pinned", "verified"):
        lib.promote(name, lib.run_tests(name, _Sandbox()))
    assert lib.trust("pinned", approved=True).state == "trusted"
    assert lib.trust("verified", approved=True).state == "trusted"


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


class _FreezeSandbox:
    def exec(self, *cmd, timeout=None):
        if "freeze" in cmd:
            return _Proc(stdout="requests==2.32.3\nnumpy==1.26.0\n")
        return _Proc()

    def snapshot_filesystem(self):
        return "snapshot-image"

    def terminate(self):
        pass


class _FreezeFactory:
    def __call__(self, network, image):
        return _FreezeSandbox()


def test_builder_records_resolved_pip(tmp_path):
    builder = CapabilityImageBuilder(tmp_path,
                                     sandbox_factory=_FreezeFactory())
    builder._base_image = lambda: "base-image"
    result = builder.build("env", pip_packages=["requests"])
    assert result["record"]["resolved_pip"] == {
        "requests": "2.32.3", "numpy": "1.26.0"}


def test_modal_backend_cpu_memory_defaults_and_create(tmp_path, monkeypatch):
    backend = ModalBackend(cpu=2.0, memory_mb=2048, image=object())
    assert backend.cpu == 2.0 and backend.memory_mb == 2048
    assert ModalBackend().cpu == 1.0
    assert ModalBackend().memory_mb == 1024
    seen = {}

    class _FakeSandbox:
        pass

    class _FakeApp:
        @staticmethod
        def lookup(name, create_if_missing=False):
            return object()

    class _FakeSandboxNS:
        @staticmethod
        def create(**kwargs):
            seen.update(kwargs)
            return _FakeSandbox()

    fake_modal = SimpleNamespace(App=_FakeApp, Sandbox=_FakeSandboxNS)
    monkeypatch.setitem(sys.modules, "modal", fake_modal)
    backend._ensure_sandbox()
    assert seen["cpu"] == 2.0
    assert seen["memory"] == 2048
    assert make_backend("modal", None, modal_cpu=3.0,
                        modal_memory_mb=4096).cpu == 3.0
