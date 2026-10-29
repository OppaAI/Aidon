"""Thinking tokens and fast-tier tokens in run totals.

- Thinking is internal/encrypted on Meta, but its count arrives
  in usage (completion_tokens_details.reasoning_tokens). The
  trace meta line shows it, and the finished-run total adds it:
  the chars//4 estimate basis (prompt + reply text) never
  contains thinking text, so adding the real reasoning count
  cannot double-count it.
- The finished total sums every counting tier, deduped, so
  fast-tier calls count even when only the router holds
  counting tiers.
"""

from src.agentic.counting import CountingClient
from src.agentic.loop import GoalRunner, Plan, RunReport, format_run_stats
from src.agentic.router import RoutedClient
from src.system.model_client import ChatResponse, FakeClient, Message
from src.system.model_router import RouterClient


class _UsageClient:
    """Inner client reporting real usage with reasoning tokens."""

    def __init__(self, reasoning):
        self.reasoning = reasoning

    def chat(self, messages, sampling=None):
        return "reply text here"

    def chat_response(self, messages, sampling=None):
        return ChatResponse(
            text="reply text here",
            usage={"prompt_tokens": 100, "completion_tokens": 50,
                   "completion_tokens_details":
                       {"reasoning_tokens": self.reasoning}})


class _PlainClient:
    def chat(self, messages, sampling=None):
        return "plain"


def _counting():
    return CountingClient(FakeClient("ok reply"))


def _msgs():
    return [Message("user", "hello world, this is a test message")]


def _runner(client, router=None):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = client
    runner.router = router
    return runner


def test_thinking_total_accumulates_and_survives_drain():
    client = CountingClient(_UsageClient(30))
    client.chat(_msgs(), phase="plan")
    assert client.take_phase_usage("plan")["thinking_tokens"] == 30
    assert client.thinking_tokens_total == 30
    client.chat(_msgs(), phase="plan")
    assert client.thinking_tokens_total == 60
    assert client.take_phase_usage("plan")["thinking_tokens"] == 30


def test_usage_totals_include_thinking():
    client = CountingClient(_UsageClient(30))
    client.chat(_msgs(), phase="plan")
    totals = _runner(client)._usage_totals()
    assert totals["calls"] == 1
    assert totals["think"] == 30
    assert totals["total"] == client.tokens_estimated + 30
    assert totals["in"] + totals["out"] == client.tokens_estimated


def test_estimate_split_sums_to_total():
    client = _counting()
    client.chat(_msgs(), phase="plan")
    client.chat(_msgs(), phase="verify")
    assert client.in_tokens_estimated + client.out_tokens_estimated \
        == client.tokens_estimated
    assert client.in_tokens_estimated > 0
    assert client.out_tokens_estimated > 0


def test_format_run_stats_breakdown():
    report = RunReport("done", "goal",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       True, True)
    report.elapsed_seconds = 82.0
    report.model_tokens_in_estimated = 1200
    report.model_tokens_out_estimated = 3400
    report.model_thinking_tokens = 800
    report.model_tokens_estimated = 5400
    text = format_run_stats(report)
    assert text.startswith("⏱️")
    assert "IN: 1,200 OUT: 3,400 (Think: 800) " \
        "TOT: 5,400 tokens (estimated)" in text


def test_format_run_stats_fallback_without_breakdown():
    report = RunReport("done", "goal",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       True, True)
    report.model_tokens_estimated = 1234
    assert "1,234 total workflow tokens (estimated)" \
        in format_run_stats(report)


def test_usage_totals_dedupe_shared_tiers():
    fast = _counting()
    strong = _counting()
    routed = RoutedClient(fast, strong, fast_phases={"plan"})
    routed.chat_phase("plan", _msgs())
    routed.chat_phase("verify", _msgs())
    runner = _runner(routed,
                     RouterClient(clients={"plan": fast}, default=strong))
    totals = runner._usage_totals()
    assert totals["calls"] == 2
    assert totals["total"] == fast.tokens_estimated + strong.tokens_estimated
    assert totals["in"] == fast.in_tokens_estimated + strong.in_tokens_estimated
    assert totals["out"] == fast.out_tokens_estimated + strong.out_tokens_estimated


def test_usage_totals_count_router_only_tiers():
    fast = _counting()
    router = RouterClient(clients={"respond": fast}, default=_counting())
    router.chat_phase("respond", _msgs())
    totals = _runner(_PlainClient(), router)._usage_totals()
    assert totals["calls"] == 1
    assert totals["total"] == fast.tokens_estimated


def test_usage_totals_fallback_to_plain_client_fields():
    plain = _PlainClient()
    plain.calls = 3
    plain.tokens_estimated = 400
    assert _runner(plain)._usage_totals() == {
        "calls": 3, "in": 0, "out": 0, "think": 0, "total": 400}
    assert _runner(_PlainClient())._usage_totals() == {
        "calls": 0, "in": 0, "out": 0, "think": 0, "total": 0}
