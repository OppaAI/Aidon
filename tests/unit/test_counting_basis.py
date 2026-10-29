"""Real vs estimated token counting and the footer basis.

Real provider usage and chars//4 estimates are tracked
separately and never added into one number. A finished-run
total uses real counts when every call reported usage
(basis "actual", agreeing with the trace rows) and the
estimate otherwise (basis "estimated"). Streamed calls
use provider usage when the stream carries it, and stay
estimated otherwise.
"""

from src.agentic.counting import CountingClient
from src.agentic.loop import GoalRunner, Plan, RunReport, format_run_stats
from src.system.model_client import ChatResponse, FakeClient, Message


class _UsageClient:
    def __init__(self, reasoning=0):
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


def _msgs():
    return [Message("user", "hello world, this is a test message")]


def _runner(client, router=None):
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = client
    runner.router = router
    return runner


def test_real_counters_track_provider_usage_only():
    client = CountingClient(_UsageClient(reasoning=30))
    client.chat(_msgs(), phase="plan")
    assert client.in_tokens_actual == 100
    assert client.out_tokens_actual == 50
    assert client.calls_with_usage == 1
    assert client.thinking_tokens_total == 30
    assert client.in_tokens_estimated > 0  # estimate still tracked


def test_plain_calls_contribute_no_actual():
    client = CountingClient(_PlainClient())
    client.chat(_msgs(), phase="plan")
    assert client.in_tokens_actual == 0
    assert client.out_tokens_actual == 0
    assert client.calls_with_usage == 0
    assert client.report()["model_tokens_basis"] == "estimated"


def test_usage_totals_basis_actual_when_all_calls_report():
    client = CountingClient(_UsageClient(reasoning=10))
    client.chat(_msgs(), phase="plan")
    client.chat(_msgs(), phase="verify")
    totals = _runner(client)._usage_totals()
    assert totals["basis"] == "actual"
    assert totals["in"] == 200
    assert totals["out"] == 100
    assert totals["think"] == 20
    assert totals["total"] == 320


def test_usage_totals_basis_estimated_when_any_call_lacks_usage():
    from src.system.model_router import RouterClient
    real = CountingClient(_UsageClient())
    plain = CountingClient(_PlainClient())
    router = RouterClient(clients={"plan": real}, default=plain)
    router.chat_phase("plan", _msgs())
    router.chat_phase("verify", _msgs())
    totals = _runner(_PlainClient(), router)._usage_totals()
    assert totals["calls"] == 2
    assert totals["basis"] == "estimated"
    assert totals["in"] == real.in_tokens_estimated + plain.in_tokens_estimated
    assert totals["out"] == real.out_tokens_estimated + plain.out_tokens_estimated
    assert totals["in_actual"] == 100
    assert totals["out_actual"] == 50


def test_streamed_calls_stay_estimated():
    client = CountingClient(FakeClient("streamed reply"))
    list(client.chat_stream(_msgs()))
    assert client.calls_with_usage == 0
    assert client.report()["model_tokens_basis"] == "estimated"


def test_footer_labels_actual_basis():
    report = RunReport("done", "goal",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       True, True)
    report.model_tokens_in_estimated = 100
    report.model_tokens_out_estimated = 50
    report.model_thinking_tokens = 10
    report.model_tokens_estimated = 160
    report.model_tokens_basis = "actual"
    text = format_run_stats(report)
    assert "IN: 100 OUT: 50 (Think: 10) TOT: 160 tokens (actual)" in text


def test_footer_defaults_to_estimated_label():
    report = RunReport("done", "goal",
                       Plan(steps=[], deliverables=[], criteria=[]),
                       True, True)
    report.model_tokens_in_estimated = 1200
    report.model_tokens_out_estimated = 3400
    report.model_thinking_tokens = 800
    report.model_tokens_estimated = 5400
    text = format_run_stats(report)
    assert "TOT: 5,400 tokens (estimated)" in text
