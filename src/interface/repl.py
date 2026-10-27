"""REPL, old Aidon repl concept, rebuilt.

A REPL is a read, eval, print loop, with history, not a one-shot chat: /help lists commands,
/history shows past goals, /clear clears history, /quit exits, and any other line is passed
to a handler function, which the caller supplies, so the loop itself never calls a model
directly. In the CLI, the handler runs a GoalRunner goal, in tests it is a fake handler, and
the loop logic is the same.

Interrupt survival: KeyboardInterrupt during input or during the handler prints "^C" and the
session continues; an interrupted goal is abandoned and never enters history. EOF still ends
the session.

Attachment staging: /attach <path> records a path without reading or copying it (a path that
does not exist is still staged, flagged "(missing)"); /staged lists staged paths;
/clear-staged drops them. When a goal runs, the handler receives the staged list as a second
argument when it accepts one, otherwise just the goal line; staged files are cleared after
the goal runs, whether it succeeds or errors.
"""

from __future__ import annotations

import inspect
import os


class Repl:
    def __init__(self, handler, input_fn=input, output_fn=print):
        self.handler = handler
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.history: list[str] = []
        self.staged: list[str] = []

    def _read_line(self) -> str | None:
        """Read one line: returns None on EOF, raises nothing on interrupt."""
        try:
            try:
                line = self.input_fn("aidon> ").strip()
            except TypeError:
                line = self.input_fn().strip()
        except EOFError:
            return None
        except KeyboardInterrupt:
            self.output_fn("^C")
            return ""
        return line

    def _call_handler(self, line: str):
        """Call the handler with (line, staged) when it accepts two arguments."""
        staged = list(self.staged)
        try:
            params = list(inspect.signature(self.handler).parameters.values())
            takes_two = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params) or len(
                [p for p in params if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                                                inspect.Parameter.POSITIONAL_OR_KEYWORD)]) >= 2
        except (TypeError, ValueError):
            takes_two = False
            try:
                return self.handler(line, staged)
            except TypeError:
                return self.handler(line)
        if takes_two:
            return self.handler(line, staged)
        return self.handler(line)

    def run(self) -> list[str]:
        self.output_fn("Aidon REPL, /help for commands, /quit to exit.")
        while True:
            line = self._read_line()
            if line is None:
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/help":
                self.output_fn("/help, /history, /clear, /attach <path>, /staged, "
                               "/clear-staged, /quit, or a goal to run.")
                continue
            if line == "/history":
                for item in self.history:
                    self.output_fn(item)
                continue
            if line == "/clear":
                self.history.clear()
                self.output_fn("History cleared.")
                continue
            if line == "/staged":
                if self.staged:
                    for path in self.staged:
                        self.output_fn(path)
                else:
                    self.output_fn("(none staged)")
                continue
            if line == "/clear-staged":
                self.staged.clear()
                self.output_fn("Staged cleared.")
                continue
            if line == "/attach" or line.startswith("/attach "):
                path = line[len("/attach"):].strip()
                if not path:
                    self.output_fn("Usage: /attach <path>")
                    continue
                self.staged.append(path)
                note = "" if os.path.exists(path) else " (missing)"
                self.output_fn(f"Staged: {path}{note}")
                continue
            try:
                reply = self._call_handler(line)
            except KeyboardInterrupt:
                self.output_fn("^C")
                self.staged.clear()
                continue
            except Exception as exc:
                reply = f"Error: {type(exc).__name__}: {exc}"
            finally:
                self.staged.clear()
            self.history.append(line)
            self.output_fn(reply)
        return self.history
