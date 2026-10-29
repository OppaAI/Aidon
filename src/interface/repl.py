"""REPL, Aidon repl concept, rebuilt.

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
import sys


class Repl:
    def __init__(self, handler, input_fn=input, output_fn=print,
                 title="Aidon REPL", on_clear=None, on_remember=None,
                 on_think=None):
        self.handler = handler
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.title = title
        self.on_clear = on_clear
        self.on_remember = on_remember
        self.on_think = on_think
        self.history: list[str] = []
        self.staged: list[str] = []
        if getattr(sys.stdout, "isatty", lambda: False)():
            self.reset = "\033[0m"
            self.bold = "\033[1m"
            self.blue = "\033[38;5;33m"
            self.cyan = "\033[38;5;51m"
            self.violet = "\033[38;5;141m"
            self.green = "\033[38;5;78m"
            self.amber = "\033[38;5;220m"
            self.red = "\033[38;5;203m"
        else:
            self.reset = self.bold = self.blue = self.cyan = ""
            self.violet = self.green = self.amber = self.red = ""

    def _paint(self, colour: str, text: str) -> str:
        return f"{colour}{text}{self.reset}" if colour else text

    def _read_line(self) -> str | None:
        """Read one line: returns None on EOF, raises nothing on interrupt."""
        try:
            try:
                line = self.input_fn(self._paint(self.cyan, "you> ")).strip()
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
        self.output_fn(self._paint(self.blue + self.bold,
                                   f"  {self.title}"))
        self.output_fn(self._paint(self.violet,
                                   "  /help for commands · /quit to leave"))
        while True:
            line = self._read_line()
            if line is None:
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/help":
                from .surfaces import CLI_HELP_TEXT
                self.output_fn(CLI_HELP_TEXT)
                continue
            if line == "/think" or line.startswith("/think "):
                # Bare /think toggles the session flag (the caller
                # owns the state via on_think); "/think <goal>" falls
                # through to the handler, which strips the prefix and
                # runs that one goal with max thinking.
                if line.strip() == "/think":
                    if self.on_think is None:
                        self.output_fn("Think toggle is unavailable in this session.")
                    else:
                        try:
                            self.output_fn(str(self.on_think()))
                        except Exception as exc:
                            self.output_fn(f"Error toggling think: {exc}")
                    continue
            if line == "/remember" or line.startswith("/remember "):
                text = line[len("/remember"):].strip()
                if not text:
                    self.output_fn("Usage: /remember <fact>")
                    continue
                if self.on_remember is None:
                    self.output_fn("Remember is unavailable in this session.")
                else:
                    try:
                        self.output_fn(str(self.on_remember(text)))
                    except Exception as exc:
                        self.output_fn(f"Error remembering fact: {exc}")
                continue
            if line == "/history":
                for item in self.history:
                    self.output_fn(item)
                continue
            if line == "/clear":
                self.history.clear()
                if self.on_clear is not None:
                    self.on_clear()
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
                if self.red:
                    self.output_fn(self._paint(self.red, f"✗ {reply}"))
                    self.staged.clear()
                    self.history.append(line)
                    continue
            finally:
                self.staged.clear()
            self.history.append(line)
            prefix = self._paint(self.green, "aidon> ") if self.green else ""
            self.output_fn(prefix + str(reply))
        return self.history
