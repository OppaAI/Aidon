"""Tool discovery, old Aidon tool_discovery concept, rebuilt.

Discovery is search over the live registry, not a hand-written list: list_tools returns
sandbox plus plugin tools, search_tools returns tools whose name or description contains
the query, case-insensitive, so a goal can find a tool it did not know by name.
No tool is invented here, if it is not in the registry, it is not discovered.
"""

from __future__ import annotations


def list_tools(home) -> list[dict]:
    from .plugins import PluginRegistry
    registry = PluginRegistry(home)
    registry.load_all()
    tools = [{"name": name, "source": "sandbox"} for name in ("write", "read", "list", "shell", "calculate")]
    for plugin in registry.load_all():
        for tool in plugin.tools:
            tools.append({"name": tool.get("name", ""), "source": plugin.name, "description": tool.get("description", "")})
    return tools


def search_tools(home, query: str) -> list[dict]:
    query = (query or "").lower()
    return [t for t in list_tools(home) if query in t["name"].lower() or query in t.get("description", "").lower()]
