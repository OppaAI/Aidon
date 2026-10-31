"""Gap 5: plugin scan bypasses closed. Gap 4: unpinned
dependencies block adoption."""

import json

import pytest

from src.capabilities.plugins import (
    PluginRegistry, scan_plugin)


def _scan(tmp_path, code="", requirements=None):
    plug = tmp_path / "plug"
    plug.mkdir(exist_ok=True)
    if code:
        (plug / "tool.py").write_text(code)
    if requirements is not None:
        (plug / "requirements.txt").write_text(requirements)
    return scan_plugin(plug)


def _types(findings):
    return {f.type for f in findings}


@pytest.mark.parametrize("code", [
    "from subprocess import run\nrun(['ls'])\n",
    "import subprocess as sp\nsp.run(['ls'])\n",
    "import os\nos.spawnlp(0, 'x')\n",
    "import os\nos.execv('/bin/x', ['x'])\n",
])
def test_subprocess_bypasses_blocked(tmp_path, code):
    assert "subprocess" in _types(_scan(tmp_path, code))


@pytest.mark.parametrize("code", [
    "x = __import__('os')\n",
    "import importlib\nimportlib.import_module('os')\n",
    "from importlib import import_module\nimport_module('os')\n",
    "import importlib.metadata as m\n",
])
def test_dynamic_import_blocked(tmp_path, code):
    assert "dynamic_import" in _types(_scan(tmp_path, code))


def test_legitimate_code_not_flagged(tmp_path):
    code = ("d = {}\nvalue = d.get('x')\n\n"
            "class Runner:\n    def run(self):\n        return 1\n"
            "Runner().run()\n")
    assert _scan(tmp_path, code) == []


def test_pinned_requirements_clean_unpinned_flagged(tmp_path):
    assert "unpinned_dependency" not in _types(
        _scan(tmp_path, requirements="requests==2.32.3\n"))
    assert "unpinned_dependency" in _types(
        _scan(tmp_path, requirements="requests\n"))
    assert "unpinned_dependency" in _types(
        _scan(tmp_path, requirements="requests>=2.0\n"))


def test_adopt_refuses_unpinned_dependencies(tmp_path):
    home = tmp_path / "home"
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / "plugin.json").write_text(json.dumps({
        "name": "unpinned-plug", "version": "1",
        "description": "test",
        "tools": [{"name": "echo", "module": "tool.py",
                   "function": "echo"}],
        "skills": []}))
    (plug / "tool.py").write_text("def echo(args):\n    return 'hi'\n")
    (plug / "requirements.txt").write_text("requests\n")
    ok, findings, message = PluginRegistry(home).adopt(plug)
    assert ok is False
    assert "unpinned_dependency" in _types(findings)
    assert "unpinned_dependency" in message
    assert not (home / "toolbox" / "unpinned-plug").exists()
