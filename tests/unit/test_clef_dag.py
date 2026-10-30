"""Clef DAG matching: winner reorder, veto, and fail-safe rules."""

from types import SimpleNamespace

from src.agentic.clef_dag import rank_dags


def _exp(goal, deliverables=(), tools=()):
    return SimpleNamespace(
        goal=goal,
        dag_nodes=[{"deliverable": d} for d in deliverables],
        tools_used=list(tools))


class _StubClef:
    def __init__(self, choice, confidence, error=None):
        self.choice = choice
        self.confidence = confidence
        self.error = error
        self.calls = []

    def decide_choice(self, state, name, instructions, criteria):
        self.calls.append((state, name, criteria))
        if self.error is not None:
            raise self.error
        assert self.choice in criteria
        return self.choice, self.confidence, {}


def test_winner_moves_to_front_rest_keep_order():
    exps = [_exp("make snake game", ("snake.py",)),
            _exp("make worm game", ("worm.py",)),
            _exp("write report", ("report.md",))]
    stub = _StubClef("dag_2", 0.9)
    ranked = rank_dags("make a worm game", exps, stub)
    assert [e.goal for e in ranked] == [
        "make worm game", "make snake game", "write report"]
    criteria = stub.calls[0][2]
    assert "worm.py" in criteria["dag_2"]
    assert criteria["none"].startswith("No candidate")


def test_low_confidence_keeps_recalled_order():
    exps = [_exp("a"), _exp("b")]
    ranked = rank_dags("goal", exps, _StubClef("dag_2", 0.4))
    assert ranked == exps


def test_high_confidence_none_vetoes_adaptation():
    exps = [_exp("a"), _exp("b")]
    ranked = rank_dags("goal", exps, _StubClef("none", 0.95))
    assert ranked == []


def test_uncertain_none_keeps_recalled_order():
    exps = [_exp("a"), _exp("b")]
    ranked = rank_dags("goal", exps, _StubClef("none", 0.7))
    assert ranked == exps


def test_error_and_no_client_and_single_candidate_unchanged():
    exps = [_exp("a"), _exp("b")]
    assert rank_dags("goal", exps, _StubClef("dag_2", 0.9,
                                             error=RuntimeError("down"))) == exps
    assert rank_dags("goal", exps, None) == exps
    single = [_exp("only")]
    stub = _StubClef("dag_1", 0.9)
    assert rank_dags("goal", single, stub) == single
    assert stub.calls == [], "no call when nothing to rank"


def test_candidate_list_capped_at_five():
    exps = [_exp(f"g{i}") for i in range(8)]
    stub = _StubClef("dag_5", 0.9)
    ranked = rank_dags("goal", exps, stub)
    assert ranked[0].goal == "g4"
    assert len(stub.calls[0][2]) == 6, "five candidates plus none"
