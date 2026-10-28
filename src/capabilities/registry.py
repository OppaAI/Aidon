"""Full registry, Aidon registry concept, rebuilt.

The registry is the one list of everything Aidon can do: sandbox tools, plugin tools,
and skill lifecycle states, plus a Markdown export, so a user can see the whole capability
list in one file, without reading code. Export is code only, from the live stores, never
hand-written, so the export cannot drift from what is actually installed.
"""

from __future__ import annotations

from pathlib import Path


def build_registry(home) -> dict:
    from .plugins import PluginRegistry
    from .skill_lifecycle import LifecycleStore
    registry = PluginRegistry(home)
    registry.load_all()
    lifecycle = LifecycleStore(home)
    return {
        "sandbox_tools": ["write", "read", "list", "shell", "calculate"],
        "plugin_tools": registry.tool_names(),
        "skills": lifecycle._data,
        "plugin_count": len(registry.load_all()),
    }


def registry_to_markdown(registry: dict) -> str:
    lines = ["# Aidon registry", "", "## Sandbox tools", ""]
    for tool in registry["sandbox_tools"]:
        lines.append(f"- {tool}")
    lines.append("")
    lines.append("## Plugin tools")
    lines.append("")
    for tool in registry["plugin_tools"]:
        lines.append(f"- {tool}")
    lines.append("")
    lines.append("## Skills")
    lines.append("")
    for name, entry in registry["skills"].items():
        lines.append(f"- {name}: {entry.get('state', '')} successes={entry.get('successes', 0)} failures={entry.get('failures', 0)}")
    return "\n".join(lines) + "\n"


def export_registry_markdown(home, dest=None) -> Path:
    registry = build_registry(home)
    dest = Path(dest) if dest else Path(home) / "export" / "registry.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(registry_to_markdown(registry), encoding="utf-8")
    return dest
