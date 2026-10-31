"""Clef client: response parsing, auth headers, and warmup."""

from src.agentic.clef_client import ClefClient


def _transport_factory(calls, response=None, error=None):
    def _transport(url, payload, headers):
        calls.append({"url": url, "payload": payload, "headers": headers})
        if error is not None:
            raise error
        return response
    return _transport


def test_decide_choice_uses_choice_field_and_confidence():
    calls = []
    resp = {"answers": {"route": {"type": "choice", "choice": "webchat",
                                  "confidence": 0.91,
                                  "probabilities": {"chat": 0.05, "webchat": 0.91,
                                                    "agentic": 0.03, "artifact": 0.01}}}}
    client = ClefClient("https://example.modal.run",
                        transport=_transport_factory(calls, resp))
    choice, conf, probs = client.decide_choice(
        {"message": "hi"}, "route", "Pick.", {"chat": "c", "webchat": "w"})
    assert choice == "webchat"
    assert conf == 0.91
    assert probs["webchat"] == 0.91
    assert calls[0]["url"].endswith("/v1/systemone")
    assert calls[0]["payload"]["model"] == "Cloudflare/clef-flash"


def test_decide_choice_falls_back_to_max_probability():
    calls = []
    resp = {"answers": {"route": {"type": "choice",
                                  "probabilities": {"chat": 0.2, "agentic": 0.8}}}}
    client = ClefClient("https://example.modal.run",
                        transport=_transport_factory(calls, resp))
    choice, conf, _ = client.decide_choice(
        {"message": "hi"}, "route", "Pick.", {"chat": "c", "agentic": "a"})
    assert choice == "agentic"
    assert conf == 0.8


def test_decide_choice_rejects_unknown_key():
    calls = []
    resp = {"answers": {"route": {"type": "choice", "choice": "other",
                                  "confidence": 0.9, "probabilities": {}}}}
    client = ClefClient("https://example.modal.run",
                        transport=_transport_factory(calls, resp))
    try:
        client.decide_choice({"message": "hi"}, "route", "Pick.",
                             {"chat": "c", "agentic": "a"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected RuntimeError for unknown choice key")


def test_decide_error_propagates_for_caller_fallback():
    calls = []
    client = ClefClient("https://example.modal.run",
                        transport=_transport_factory(
                            calls, error=RuntimeError("boom")))
    try:
        client.decide_choice({"message": "hi"}, "route", "Pick.", {"chat": "c"})
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected transport error to propagate")


def test_proxy_auth_headers_only_when_both_parts_set():
    calls = []
    resp = {"answers": {"route": {"type": "choice", "choice": "chat",
                                  "confidence": 1.0, "probabilities": {}}}}
    client = ClefClient("https://example.modal.run", key="k", secret="s",
                        transport=_transport_factory(calls, resp))
    client.decide_choice({"message": "hi"}, "route", "Pick.", {"chat": "c"})
    assert calls[0]["headers"]["Modal-Key"] == "k"
    assert calls[0]["headers"]["Modal-Secret"] == "s"
    calls.clear()
    client2 = ClefClient("https://example.modal.run", key="k",
                         transport=_transport_factory(calls, resp))
    client2.decide_choice({"message": "hi"}, "route", "Pick.", {"chat": "c"})
    assert calls[0]["headers"] == {}


def test_warmup_is_one_small_call_and_ensure_warmed_is_once():
    calls = []
    resp = {"answers": {"warmup": {"type": "choice", "choice": "ok",
                                   "confidence": 1.0, "probabilities": {}}}}
    client = ClefClient("https://example.modal.run",
                        transport=_transport_factory(calls, resp))
    client.warmup()
    assert calls[0]["payload"]["questions"].keys() == {"warmup"}
    calls.clear()
    thread = client.ensure_warmed()
    thread.join(timeout=5)
    client.ensure_warmed()
    assert len(calls) == 1
