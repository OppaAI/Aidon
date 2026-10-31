"""Prompt injection: the ordinary model-call path (_chat)
prepends build_identity_prompt, so the manual sections
that path carries are what the model actually receives."""

from src.agentic import prompts
from src.agentic.prompts import SYSTEM_LINE, build_identity_prompt


def test_identity_carries_manual_sections():
    out = build_identity_prompt("plan")
    assert SYSTEM_LINE in out
    assert "You are in the plan phase." in out
    for heading in ("## The loop", "## Writing tool plugins",
                    "## Capability ladder", "## Hard rules"):
        assert heading in out, heading
    # Distinctive manual content, not just headings.
    assert "adopt_tool_plugin" in out
    # The plugin format and MCP rung must match the real loader and
    # the real mcp_connect tool, not an older file-based interface.
    assert "plugin.json" in out
    assert "mcp_connect" in out
    assert "build_tools" not in out
    assert "no persistent connect step" not in out


def test_identity_without_manual_is_graceful(monkeypatch):
    orig = prompts._read_doc

    def fake(name):
        return "" if name == "AGENTS.md" else orig(name)

    monkeypatch.setattr(prompts, "_read_doc", fake)
    out = build_identity_prompt("plan")
    assert SYSTEM_LINE in out
    assert "## Capability ladder" not in out


def test_sections_come_from_manual_not_hardcoded(monkeypatch):
    orig = prompts._read_doc
    fake_manual = (
        "## The loop\nMARKER-LOOP\n\n"
        "## Writing tool plugins\nMARKER-PLUGINS\n\n"
        "## Capability ladder\nMARKER-LADDER\n\n"
        "## Hard rules\nMARKER-RULES\n")

    def fake(name):
        return fake_manual if name == "AGENTS.md" else orig(name)

    monkeypatch.setattr(prompts, "_read_doc", fake)
    out = build_identity_prompt("plan")
    for marker in ("MARKER-LOOP", "MARKER-PLUGINS",
                   "MARKER-LADDER", "MARKER-RULES"):
        assert marker in out, marker
