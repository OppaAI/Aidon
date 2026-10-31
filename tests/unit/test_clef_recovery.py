"""Clef recovery router: route mapping, thresholds, and state caps."""

from src.agentic.clef_recovery import MAX_FAILURE_CHARS, route_failure


class _StubClef:
    def __init__(self, route, confidence, error=None):
        self.route = route
        self.confidence = confidence
        self.error = error
        self.calls = []

    def decide_choice(self, state, name, instructions, criteria):
        self.calls.append((state, name, criteria))
        if self.error is not None:
            raise self.error
        return self.route, self.confidence, {}


def test_each_route_passes_through():
    for route in ("retry_execution", "repair_artifact",
                  "acquire_capability", "escalate"):
        result = route_failure("goal", 1, ["boom"], _StubClef(route, 0.9))
        assert result["route"] == route
        assert result["confidence"] == 0.9


def test_unknown_route_falls_back_to_repair():
    result = route_failure("goal", 1, ["boom"],
                           _StubClef("teleport", 0.99))
    assert result["route"] == "repair_artifact"


def test_no_client_repairs_as_today():
    result = route_failure("goal", 1, ["boom"], None)
    assert result["route"] == "repair_artifact"
    assert result["confidence"] == 0.0


def test_error_propagates_for_caller_fallback():
    try:
        route_failure("goal", 1, ["boom"],
                      _StubClef("retry_execution", 0.9,
                                error=RuntimeError("down")))
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected transport error to propagate")


def test_failure_text_capped():
    stub = _StubClef("repair_artifact", 0.9)
    route_failure("goal", 1, ["x" * 5000], stub)
    state = stub.calls[0][0]
    assert len(state["failures"]) <= MAX_FAILURE_CHARS
    assert state["attempt"] == 1
