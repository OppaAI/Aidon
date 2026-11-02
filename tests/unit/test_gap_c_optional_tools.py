"""Commit C: optional-tools flip, default off = minimal core only.

The flip gates built-in extra tools only. Adopted plugins and
trusted skills are earned tools and are never gated by it."""

from types import SimpleNamespace

from src.agentic.kernel import CORE_LADDER_TOOLS
from src.system.config import Config
from src.system.main import _goal_extra_tools

CORE = set(CORE_LADDER_TOOLS) | {"read_file", "write_file"}
OPTIONAL = {"youtube_upload", "threads_publish",
            "generate_document"}
CONDITIONAL = {"computer_use", "computer_use_task"}
ALWAYS_AVAILABLE = {"edit_image"}


def _tools(tmp_path, **kwargs):
    return _goal_extra_tools(SimpleNamespace(home=tmp_path), tmp_path,
                             **kwargs)


def test_on_shows_full_set(tmp_path):
    tools = _tools(tmp_path, optional_tools=True)
    # Skill adoption is a post-success Skill Lab capability, never a
    # generally available tool in ordinary work or optional-tools mode.
    assert "adopt_skill" not in tools
    for name in OPTIONAL:
        assert name in tools, name
    for name in CORE - CONDITIONAL:
        if name != "adopt_skill":
            assert name in tools, name


def test_off_shows_core_only(tmp_path):
    on = _tools(tmp_path, optional_tools=True)
    off = _tools(tmp_path, optional_tools=False)
    assert set(off) <= (CORE | ALWAYS_AVAILABLE)
    assert set(off) >= (set(on) & CORE)
    assert ALWAYS_AVAILABLE <= set(off)
    for name in OPTIONAL:
        assert name not in off, name


def test_legacy_caller_without_field_keeps_full_set(tmp_path):
    # Callers that predate the field (and tests using a bare
    # SimpleNamespace) keep the full set: backward compatible.
    tools = _tools(tmp_path)
    for name in OPTIONAL:
        assert name in tools, name


def test_config_default_off_env_on(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDON_OPTIONAL_TOOLS", raising=False)
    assert Config.load(tmp_path).optional_tools is False
    monkeypatch.setenv("AIDON_OPTIONAL_TOOLS", "1")
    assert Config.load(tmp_path).optional_tools is True
    monkeypatch.setenv("AIDON_OPTIONAL_TOOLS", "off")
    assert Config.load(tmp_path).optional_tools is False
