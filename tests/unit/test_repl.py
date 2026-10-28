"""REPL extras: interrupt survival, attachment staging, more slash commands."""

import pytest

from src.interface.repl import Repl


def _drive(lines, handler):
    """Feed lines (items may be exceptions to raise from input), run, return (out, repl)."""
    feed = list(lines)
    out = []

    def input_fn(prompt=""):
        if not feed:
            raise EOFError
        item = feed.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    repl = Repl(handler, input_fn=input_fn, output_fn=out.append)
    history = repl.run()
    return out, history, repl


def test_input_interrupt_does_not_end_session():
    seen = []

    def handler(line):
        seen.append(line)
        return f"done: {line}"

    out, history, _ = _drive([KeyboardInterrupt(), "hello", "/quit"], handler)
    assert seen == ["hello"]
    assert history == ["hello"]
    assert "^C" in out


def test_handler_interrupt_abandons_goal_and_continues():
    seen = []

    def handler(line):
        if line == "boom":
            raise KeyboardInterrupt
        seen.append(line)
        return f"done: {line}"

    out, history, _ = _drive(["boom", "next", "/quit"], handler)
    assert seen == ["next"]
    assert history == ["next"]
    assert "^C" in out


def test_attach_staged_passed_to_handler_and_cleared():
    received = []

    def handler(line, staged):
        received.append((line, list(staged)))
        return "ok"

    out, _, repl = _drive(["/attach /tmp/a.txt", "/attach /tmp/b.txt", "/staged",
                           "do it", "/staged", "/quit"], handler)
    assert received == [("do it", ["/tmp/a.txt", "/tmp/b.txt"])]
    assert repl.staged == []
    assert any(line.startswith("Staged: /tmp/a.txt") for line in out)
    assert "/tmp/a.txt" in out and "/tmp/b.txt" in out  # /staged listing
    assert "(none staged)" in out  # second /staged, after the goal cleared them


def test_one_arg_handler_still_works_with_staged():
    seen = []

    def handler(line):
        seen.append(line)
        return "ok"

    _drive(["/attach /tmp/a.txt", "goal", "/quit"], handler)
    assert seen == ["goal"]


def test_missing_staged_path_flagged(tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x")
    out, _, _ = _drive([f"/attach {real}", "/attach /tmp/definitely-not-here-xyz.txt",
                        "/quit"], lambda line: "ok")
    assert f"Staged: {real}" in out
    assert "Staged: /tmp/definitely-not-here-xyz.txt (missing)" in out


def test_staged_cleared_after_handler_error():
    def handler(line):
        raise RuntimeError("nope")

    _, _, repl = _drive(["/attach /tmp/a.txt", "goal", "/quit"], handler)
    assert repl.staged == []


def test_clear_empties_history():
    out, history, _ = _drive(["one", "/clear", "/history", "/quit"], lambda line: "ok")
    assert history == []
    assert "History cleared." in out
    assert "one" not in out[1:]  # /history printed nothing after the clear


def test_clear_staged():
    out, _, repl = _drive(["/attach /tmp/a.txt", "/clear-staged", "/staged", "/quit"],
                          lambda line: "ok")
    assert repl.staged == []
    assert "Staged cleared." in out
    assert "(none staged)" in out


def test_help_lists_new_commands_and_old_behaviour():
    out, history, _ = _drive(["/help", "goal", "/history", "/quit"], lambda line: "reply")
    assert any("/attach" in line and "/staged" in line and "/clear" in line for line in out)
    assert "reply" in out
    assert "goal" in out  # /history echoes it
    assert history == ["goal"]


def test_remember_command_uses_callback_without_running_goal():
    seen = []
    out, history, _ = _drive(["/remember I like blue", "/quit"],
                             lambda line: seen.append(line))
    assert seen == []
    assert history == []
    assert "Remember is unavailable in this session." in out


def test_eof_still_ends_session():
    out, history, _ = _drive(["goal"], lambda line: "reply")
    assert history == ["goal"]
