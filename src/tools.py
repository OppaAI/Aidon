"""Sandbox tools: the only tools the loop executes directly.

These are arena plumbing, given by construction, not
capability tools Aidon climbs to: write a file, read a
file, list files, run a command in the sandbox workdir,
and calculate safely. Every tool is confined to the
sandbox workdir: a path outside it is rejected, never
followed. Capability tools, research, email, PDF, GitHub,
and so on, are built by Aidon later, through the plugin
path, and are not in this registry.

A tool call is a dict: {"tool": name, "args": {...}}.
Args are data, never code: shell commands are a list,
never a string, and calculation uses an AST evaluator,
never eval().
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path


def _confined(workdir: Path, name: str) -> Path:
    target = (workdir / name).resolve()
    root = workdir.resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path outside workdir: {name}")
    return target


def _calc(expression: str) -> float:
    """Safe arithmetic: numbers and + - * / () only, no names, no calls."""
    tree = ast.parse(expression, mode="eval")

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.BinOp) and isinstance(
                node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left, right = walk(node.left), walk(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            return left / right
        if isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.USub, ast.UAdd)):
            value = walk(node.operand)
            return -value if isinstance(node.op, ast.USub) else value
        raise ValueError("unsafe calculation")

    return walk(tree)


@dataclass
class Tool:
    name: str
    description: str
    func: object = None


class ToolRegistry:
    def __init__(self, workdir: Path, sandbox=None):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.sandbox = sandbox
        self.tools = {
            "write": Tool("write", "Write text to a file in the workdir."),
            "read": Tool("read", "Read a file in the workdir."),
            "list": Tool("list", "List files in the workdir."),
            "shell": Tool("shell", "Run a command list in the sandbox."),
            "calculate": Tool("calculate", "Safe arithmetic, or sum a ledger."),
        }

    def call(self, name: str, args: dict) -> str:
        """Execute one tool, return text output, raise on failure."""
        if name == "write":
            target = _confined(self.workdir, str(args["file"]))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args.get("text", "")), encoding="utf-8")
            return f"wrote {args['file']}"
        if name == "read":
            target = _confined(self.workdir, str(args["file"]))
            return target.read_text(encoding="utf-8")
        if name == "list":
            return "\n".join(sorted(
                str(p.relative_to(self.workdir))
                for p in self.workdir.rglob("*") if p.is_file()))
        if name == "shell":
            if self.sandbox is None:
                raise ValueError("no sandbox for shell tool")
            command = args["command"]
            if not isinstance(command, list) or not command:
                raise ValueError("shell command must be a non-empty list")
            result = self.sandbox.run([str(c) for c in command])
            if not result.ok:
                raise ValueError(f"shell failed: {result.stderr[:200]}")
            return result.stdout
        if name == "calculate":
            if "expression" in args:
                return str(_calc(str(args["expression"])))
            if "ledger" in args:
                # Ledger: a JSON file with lines [{amount_cents}],
                # or a text file with one amount per line in cents.
                # Totals are recomputed by code, never by the model.
                target = _confined(self.workdir, str(args["ledger"]))
                raw = target.read_text(encoding="utf-8")
                try:
                    data = json.loads(raw)
                    lines = data.get("lines", [])
                    total = sum(int(line.get("amount_cents", 0)) for line in lines)
                except (json.JSONDecodeError, AttributeError, TypeError):
                    total = sum(int(line.strip()) for line in raw.splitlines()
                                if line.strip())
                return str(total)
            raise ValueError("calculate needs expression or ledger")
        raise ValueError(f"unknown tool: {name}")

    def names(self) -> list[str]:
        return sorted(self.tools)
