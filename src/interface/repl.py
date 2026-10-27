"""REPL, old Aidon repl concept, rebuilt.

A REPL is a read, eval, print loop, with history, not a one-shot chat: /help lists commands,
/history shows past goals, /quit exits, and any other line is passed to a handler function,
which the caller supplies, so the loop itself never calls a model directly. In the CLI, the
handler runs a GoalRunner goal, in tests it is a fake handler, and the loop logic is the same.
"""

from __future__ import annotations


class Repl:
    def __init__(self, handler, input_fn=input, output_fn=print):
        self.handler = handler
        self.input_fn = input_fn
        self.output_fn = output_fn
        self.history: list[str] = []

    def run(self) -> list[str]:
        self.output_fn("Aidon REPL, /help for commands, /quit to exit.")
        while True:
            try:
                try:
                    line = self.input_fn("aidon> ").strip()
                except TypeError:
                    line = self.input_fn().strip()
            except EOFError:
                break
            if not line:
                continue
            if line == "/quit":
                break
            if line == "/help":
                self.output_fn("/help, /history, /quit, or a goal to run.")
                continue
            if line == "/history":
                for item in self.history:
                    self.output_fn(item)
                continue
            self.history.append(line)
            try:
                reply = self.handler(line)
            except Exception as exc:
                reply = f"Error: {type(exc).__name__}: {exc}"
            self.output_fn(reply)
        return self.history
