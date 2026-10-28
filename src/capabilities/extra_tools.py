"""Extra loop tools helper.

The confined sandbox toolset in the loop covers write, read,
list, shell, and calculate. Social, GitHub, YouTube, and
email tools are not in that set, because they reach outside
the sandbox and, for social and email, need approval before
they act. This helper validates and dispatches such tools,
and the parent gates them by approval before dispatch.

Rules:
- Tool names must be safe identifiers: letters, digits, and
  underscore, not starting with a digit. Anything else
  raises ValueError, so a tool name can never smuggle a
  path, a command, or a lookup key.
- Every handler must be callable, otherwise ValueError.
- Dispatch with a dict calls the handler with **args.
  Dispatch with any other value calls the handler with
  that value as a single argument. Dispatch with None
  calls the handler with no arguments.
- An unknown tool name raises ValueError, it never falls
  back to another tool.
"""

from __future__ import annotations

import re

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def build_extra_tools(handlers: dict) -> dict:
    """Validate a handler dict and return a copy of it."""
    if not isinstance(handlers, dict):
        raise ValueError("handlers must be a dict")
    tools = {}
    for name, handler in handlers.items():
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(f"invalid tool name: {name!r}")
        if not callable(handler):
            raise ValueError(f"tool handler is not callable: {name!r}")
        tools[name] = handler
    return tools


def dispatch_extra_tool(tools: dict, name: str, args=None) -> str:
    """Dispatch one extra tool and return its result as a string."""
    if not isinstance(tools, dict) or name not in tools:
        raise ValueError(f"unknown extra tool: {name!r}")
    handler = tools[name]
    if args is None:
        result = handler()
    elif isinstance(args, dict):
        result = handler(**args)
    else:
        result = handler(args)
    # Preserve structured string subclasses such as ToolResult. They remain
    # fully backward-compatible strings, but carry authoritative artifact
    # metadata (path/kind/status) for the executor and verifier.
    return result if isinstance(result, str) else str(result)
