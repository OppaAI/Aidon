"""Kernel mode, Aidon, seed-only toolset, no harm, opt-in.

AIDON_KERNEL=1 means only the minimal seed toolset is
exposed: write, read, list, shell, plus research fetch,
the human approval channel, gated installs, MCP connect,
and plugin adoption, which live outside the loop tool
registry. Every other capability must be earned via
the ladder: research, draft a plugin or skill, sandbox
test it, adopt it with owner approval. AIDON_KERNEL=0,
the default, is the full built-in toolset.
No harm, because kernel mode is opt-in, off by default,
and it only restricts what the planner is told and what
the loop will execute, it does not delete plugins,
skills, or memory, and turning it off restores the full
toolset, with no state to migrate.
In the new loop, the registry tools are write, read,
list, shell, calculate. Kernel mode allows write, read,
list, shell only, and blocks calculate and all adopted
plugin tools, because those are earned capabilities,
not seed tools. A blocked tool call is an error, never
a silent skip, so a plan that needs a blocked tool
fails visibly and can be repaired with seed tools.
"""

from __future__ import annotations

SEED_TOOLS = {"write", "read", "list", "shell"}

# Built-ins kept available when optional tools are off. These include
# explicitly approval-gated host capture tools; the flip controls
# visibility, not their record approval boundary.
# When AIDON_OPTIONAL_TOOLS is off (the default), the goal
# extra tools are exactly this set plus the read_file /
# write_file step aliases, which are kept because the loop
# step primitives already include them. Everything else
# built-in is optional: invisible when off, restored by
# flipping one setting on. Adopted plugins and trusted
# skills are earned tools, never gated by this flip.
CORE_LADDER_TOOLS = frozenset({
    "fetch_url", "ask_user", "declare_blocked", "notify",
    "uv_venv", "install_python_package", "apt_install",
    "mcp_connect", "mcp_disconnect", "adopt_tool_plugin",
    "capture_webcam_photo", "record_webcam", "record_system_audio",
})


def kernel_allowed(tool_name: str, kernel_mode: bool) -> bool:
    if not kernel_mode:
        return True
    return tool_name in SEED_TOOLS


def kernel_filter_tools(tool_names: list[str], kernel_mode: bool) -> list[str]:
    if not kernel_mode:
        return list(tool_names)
    return [name for name in tool_names if name in SEED_TOOLS]


def kernel_system_note(kernel_mode: bool) -> str:
    if not kernel_mode:
        return ""
    return ("Kernel mode on: only seed tools are available: "
            "write, read, list, shell. No calculate, no plugin "
            "tools, no other capabilities, until they are earned "
            "via the plugin ladder with owner approval.")
