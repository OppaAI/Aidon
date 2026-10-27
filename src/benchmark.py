"""Self-learning benchmark: fresh run vs adapted run, same quality checks.

This is the measurement the Snake -> Worm review asked for,
in code, not as a slogan. A benchmark run does two runs with
the same quality checks (deliverable exists, non-empty, and
contains the new task word, not the old one):
- fresh: a new goal, planned from scratch, with a CountingClient,
- adapted: the same goal, with a saved Experience from a
  related goal, adapted by adapt_experience, with a CountingClient.
It reports, for each run: model calls, estimated tokens,
calls by phase, and whether the quality checks passed.
A fresh run that needs a plan call and an adapted run that
needs only an adaptation call, or no call at all for an
identical goal, is a measured saving. A run that fails its
quality checks is not counted as a saving, however few calls
it made, because a cheaper wrong answer is not learning.

The benchmark here uses injected runners, so tests run it
with fake clients and fake tool execution, no network, no
real model. A live benchmark, with a real model, is the
demo and testing part, done after work, not here.
"""

from __future__ import annotations

from .counting import CountingClient


def _quality_ok(files: dict[str, str], new_word: str, old_word: str) -> bool:
    if not files:
        return False
    text = "\n".join(files.values())
    return bool(text.strip()) and new_word.lower() in text.lower() \
        and old_word.lower() not in text.lower()


def benchmark_learn(fresh_runner, adapted_runner, new_word: str, old_word: str) -> dict:
    """fresh_runner and adapted_runner are callables returning (report, files_dict)."""
    fresh_report, fresh_files = fresh_runner()
    adapted_report, adapted_files = adapted_runner()
    fresh_calls = getattr(fresh_report, "model_calls", 0)
    adapted_calls = getattr(adapted_report, "model_calls", 0)
    fresh_tokens = getattr(fresh_report, "model_tokens_estimated", 0)
    adapted_tokens = getattr(adapted_report, "model_tokens_estimated", 0)
    return {
        "fresh": {"model_calls": fresh_calls, "model_tokens_estimated": fresh_tokens,
                  "quality_ok": _quality_ok(fresh_files, new_word, old_word),
                  "status": getattr(fresh_report, "status", "")},
        "adapted": {"model_calls": adapted_calls, "model_tokens_estimated": adapted_tokens,
                    "quality_ok": _quality_ok(adapted_files, new_word, old_word),
                    "status": getattr(adapted_report, "status", "")},
        "calls_saved": fresh_calls - adapted_calls,
        "tokens_saved_estimated": fresh_tokens - adapted_tokens,
        "saving_counts": _quality_ok(adapted_files, new_word, old_word)
        and adapted_calls < fresh_calls,
    }


def benchmark_total(learn_calls: int, learn_tokens: int, fresh_calls: int, fresh_tokens: int,
                    adapted_calls: int, adapted_tokens: int) -> dict:
    """Total cost for two tasks, learning cost included, not hidden.

    Two-task total, fresh path: learn a first task, then solve
    the second task fresh. Adapted path: learn the first task
    once, then solve the second task by adapting it. The
    learning cost is in both totals, so a saving is not
    created by counting only the cheaper second run.
    """
    fresh_total_calls = learn_calls + fresh_calls
    adapted_total_calls = learn_calls + adapted_calls
    return {"fresh_total_calls": fresh_total_calls,
            "adapted_total_calls": adapted_total_calls,
            "fresh_total_tokens_estimated": learn_tokens + fresh_tokens,
            "adapted_total_tokens_estimated": learn_tokens + adapted_tokens,
            "calls_saved_total": fresh_total_calls - adapted_total_calls,
            "tokens_saved_total_estimated": (learn_tokens + fresh_tokens) - (learn_tokens + adapted_tokens)}


def counting_report(client: CountingClient) -> dict:
    return client.report()
