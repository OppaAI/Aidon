"""Clef router: route/mode mapping, mode caching, safe fallback."""

from types import SimpleNamespace

from src.agentic.clef_client import ClefClient
from src.agentic.clef_router import ClefRouter


def _client(response=None, error=None, calls=None):
    def _transport(url, payload, headers):
        calls.append(payload)
        if error is not None:
            raise error
        return response
    client = ClefClient("https://example.modal.run",
                        transport=_transport)
    client.ensure_warmed = lambda: None
    return client


def _both_response(route="webchat", route_conf=0.9, mode="ask"):
    return {"answers": {
        "route": {"type": "choice", "choice": route, "confidence": route_conf,
                  "probabilities": {route: route_conf}},
        "mode": {"type": "choice", "choice": mode, "confidence": 0.8,
                 "probabilities": {mode: 0.8}}}}


def test_classify_maps_route_and_caches_mode():
    calls = []
    router = ClefRouter(_client(_both_response(), calls=calls))
    route, conf = router.classify("what is the weather today")
    assert route == "webchat"
    assert conf == 0.9
    assert router.mode_for("what is the weather today") == "ask"
    assert len(calls) == 1, "mode_for must reuse the classify call"


def test_mode_for_without_cache_asks_mode_only():
    calls = []
    router = ClefRouter(_client(_both_response(mode="plan"), calls=calls))
    assert router.mode_for("plan my week") == "plan"
    assert len(calls) == 1


def test_mode_neither_maps_to_none():
    calls = []
    router = ClefRouter(_client(_both_response(mode="neither"), calls=calls))
    route, _ = router.classify("do it")
    assert route == "webchat"
    assert router.mode_for("do it") is None


def test_error_falls_back_to_semantic_router():
    calls = []
    fallback = SimpleNamespace(classify=lambda text: ("chat", 0.5),
                               mode_for=lambda text: "ask")
    router = ClefRouter(_client(error=RuntimeError("down"), calls=calls),
                        fallback=fallback)
    assert router.classify("hello") == ("chat", 0.5)
    assert router.mode_for("hello") == "ask"


def test_error_without_fallback_routes_agentic():
    calls = []
    router = ClefRouter(_client(error=RuntimeError("down"), calls=calls))
    assert router.classify("hello") == ("agentic", 0.0)
    assert router.mode_for("hello") is None


def test_unparseable_answer_falls_back():
    calls = []
    resp = {"answers": {"route": {"type": "choice", "choice": "nope",
                                  "confidence": 0.9, "probabilities": {}},
                        "mode": {"type": "choice", "choice": "ask",
                                 "confidence": 0.9, "probabilities": {}}}}
    fallback = SimpleNamespace(classify=lambda text: ("agentic", 0.7),
                               mode_for=lambda text: None)
    router = ClefRouter(_client(resp, calls=calls), fallback=fallback)
    assert router.classify("hello") == ("agentic", 0.7)
