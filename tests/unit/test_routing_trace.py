"""Trace tokens through routers, and the fast-model routing judge.

Fix (3): per-phase trace tokens were 0 on WebUI because routers
dropped the phase when delegating to counting tiers (everything
landed in the "chat" bucket) and _counting_clients never unwrapped
RoutedClient/RouterClient to reach the tiers at all.

Fix (4): SemanticRouter.classify gains an optional fast-model
judge, consulted only on low-confidence routes and on user-pause
messages that embed like agentic tasks.
"""

from src.agentic.counting import CountingClient
from src.agentic.loop import GoalRunner
from src.agentic.router import RoutedClient
from src.agentic.semantic_router import SemanticRouter
from src.system.model_client import FakeClient, Message
from src.system.model_router import RouterClient


# --- fake clients ---

class _PlainClient:
    """A tier without a phase parameter (old call shape)."""

    def __init__(self):
        self.calls = []

    def chat(self, messages, sampling=None):
        self.calls.append(messages)
        return "plain reply"


class _RouteStub:
    """A SemanticRouter with fully controlled embedding scores."""

    def __init__(self, vec):
        router = SemanticRouter.__new__(SemanticRouter)
        router._chat_vecs = [[0.0, 1.0]]
        router._webchat_vecs = [[-1.0, 0.0]]
        router._agentic_vecs = [[1.0, 0.0]]
        router._artifact_vecs = [[0.0, -1.0]]
        router._web_intent_vecs = [[-1.0, 0.0]]
        router._embed = lambda texts: [list(vec) for _ in texts]
        self.router = router

    def classify(self, text, judge=None):
        return self.router.classify(text, judge=judge)


class _ScriptedClient:
    """Returns one canned reply per call, in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, sampling=None):
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _counting():
    return CountingClient(FakeClient("ok reply"))


def _msgs():
    return [Message("user", "hello world, this is a test message")]


# --- fix (3): phase forwarding through routers ---

def test_routed_client_forwards_phase_to_counting_tier():
    fast = _counting()
    strong = _counting()
    routed = RoutedClient(fast, strong, fast_phases={"plan"})
    routed.chat_phase("plan", _msgs())
    delta = fast.take_phase_usage("plan")
    assert delta["tokens_in"] > 0 and delta["tokens_out"] > 0
    assert strong.take_phase_usage("plan")["tokens_in"] == 0


def test_routed_client_forwards_phase_to_strong_tier():
    fast = _counting()
    strong = _counting()
    routed = RoutedClient(fast, strong, fast_phases={"plan"})
    routed.chat_phase("verify", _msgs())
    delta = strong.take_phase_usage("verify")
    assert delta["tokens_in"] > 0 and delta["tokens_out"] > 0
    assert fast.take_phase_usage("verify")["tokens_in"] == 0


def test_router_client_forwards_phase_to_counting_tier():
    plan_tier = _counting()
    default_tier = _counting()
    router = RouterClient(clients={"plan": plan_tier}, default=default_tier)
    router.chat_phase("plan", _msgs())
    delta = plan_tier.take_phase_usage("plan")
    assert delta["tokens_in"] > 0 and delta["tokens_out"] > 0
    assert default_tier.take_phase_usage("plan")["tokens_in"] == 0


def test_routed_client_plain_tier_keeps_old_call_shape():
    plain = _PlainClient()
    routed = RoutedClient(plain, _PlainClient(), fast_phases={"plan"})
    assert routed.chat_phase("plan", _msgs()) == "plain reply"
    assert len(plain.calls) == 1


def test_counting_clients_unwraps_routers():
    fast = _counting()
    strong = _counting()
    plan_tier = _counting()
    default_tier = _counting()
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = RoutedClient(fast, strong, fast_phases={"plan"})
    runner.router = RouterClient(clients={"plan": plan_tier},
                                 default=default_tier)
    found = runner._counting_clients()
    assert fast in found and strong in found
    assert plan_tier in found and default_tier in found
    assert len(found) == len(set(map(id, found)))


def test_counting_clients_plain_client_only():
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = _counting()
    runner.router = None
    found = runner._counting_clients()
    assert len(found) == 1


# --- fix (4): fast-model routing judge ---

def test_judge_overrides_pause_message_at_high_confidence():
    # Embedding says agentic at confidence 1.0 (vec == agentic
    # example, margin maximal), but the message is the user
    # pausing to check something themselves.
    stub = _RouteStub([1.0, 0.0])
    route, _ = stub.classify(
        "Give me a minute. Let me see if if we create a file.",
        judge=lambda text: "chat")
    assert route == "chat"


def test_no_judge_keeps_pause_misroute():
    stub = _RouteStub([1.0, 0.0])
    route, conf = stub.classify(
        "Give me a minute. Let me see if if we create a file.")
    assert route == "agentic"
    assert conf == 1.0


def test_judge_not_consulted_on_clear_task():
    stub = _RouteStub([1.0, 0.0])
    seen = []
    route, _ = stub.classify(
        "Create a file for me.",
        judge=lambda text: seen.append(text) or "chat")
    assert route == "agentic"
    assert seen == []


def test_judge_overrides_low_confidence_route():
    # Equidistant vec: margin 0, confidence 0, falls back to
    # agentic; the judge rescues it.
    stub = _RouteStub([0.7071, 0.7071])
    route, _ = stub.classify("something vague", judge=lambda text: "webchat")
    assert route == "webchat"


def test_judge_failure_and_garbage_keep_embedding():
    stub = _RouteStub([1.0, 0.0])

    def boom(text):
        raise RuntimeError("fast model down")

    route, _ = stub.classify(
        "Give me a minute. Let me see if if we create a file.",
        judge=boom)
    assert route == "agentic"
    route, _ = stub.classify(
        "Give me a minute. Let me see if if we create a file.",
        judge=lambda text: "nonsense")
    assert route == "agentic"


def test_route_judge_abstains_without_fast_client():
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = _counting()
    assert runner._route_judge() is None


def test_route_judge_parses_json_and_abstains_on_chatter():
    fast = _ScriptedClient(['{"route": "chat"}',
                            "I think this is chat, maybe"])
    runner = GoalRunner.__new__(GoalRunner)
    runner.client = RoutedClient(CountingClient(fast),
                                 CountingClient(FakeClient("strong")),
                                 fast_phases={"plan"})
    judge = runner._route_judge()
    assert judge is not None
    assert judge("Give me a minute") == "chat"
    assert judge("Give me a minute") is None
