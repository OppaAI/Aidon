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
from src.agentic.loop import GoalRunner
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
    calls, tokens = _runner(client)._usage_totals()
    assert calls == 1
    assert tokens == client.tokens_estimated + 30


def test_usage_totals_dedupe_shared_tiers():
    fast = _counting()
    strong = _counting()
    routed = RoutedClient(fast, strong, fast_phases={"plan"})
    routed.chat_phase("plan", _msgs())
    routed.chat_phase("verify", _msgs())
    runner = _runner(routed,
                     RouterClient(clients={"plan": fast}, default=strong))
    calls, tokens = runner._usage_totals()
    assert calls == 2
    assert tokens == fast.tokens_estimated + strong.tokens_estimated


def test_usage_totals_count_router_only_tiers():
    fast = _counting()
    router = RouterClient(clients={"respond": fast}, default=_counting())
    router.chat_phase("respond", _msgs())
    calls, tokens = _runner(_PlainClient(), router)._usage_totals()
    assert calls == 1
    assert tokens == fast.tokens_estimated


def test_usage_totals_fallback_to_plain_client_fields():
    plain = _PlainClient()
    plain.calls = 3
    plain.tokens_estimated = 400
    assert _runner(plain)._usage_totals() == (3, 400)
    assert _runner(_PlainClient())._usage_totals() == (0, 0)
