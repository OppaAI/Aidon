"""Deep research: adaptive fetch, multi-round follow-ups, cited
evidence. No network, no model — all callables are fakes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.deep_research import (
    condense, deep_research, score_chunk)


def test_score_and_condense_keep_best_in_order():
    query = "roast beef dinner"
    assert score_chunk("roast beef with potatoes", {"roast", "beef"}) == 2
    assert score_chunk("unrelated weather", {"roast", "beef"}) == 0
    # Low-score chunks are long on purpose: with a 100-char cap
    # they cannot fit after the two on-topic chunks are kept.
    filler = "x" * 200
    text = (f"{filler}\n\nroast beef cooking times\n\n"
            f"{filler}\n\nbeef roast temperatures")
    out = condense(text, query, max_chars=100)
    assert "roast beef cooking times" in out
    assert "beef roast temperatures" in out
    assert filler not in out
    assert out.index("roast beef cooking times") < \
        out.index("beef roast temperatures")


def _results():
    return [
        {"title": "A", "url": "https://a.example/x",
         "snippet": "roast beef guide"},
        {"title": "B", "url": "https://b.example/y",
         "snippet": "beef prices"},
        {"title": "C", "url": "https://c.example/z",
         "snippet": "unrelated"},
    ]


def _pages():
    return {
        "https://a.example/x": "roast beef\n\ncook roast beef slowly",
        "https://b.example/y": "beef prices\n\nbeef costs more",
        "https://c.example/z": "unrelated\n\nnothing relevant",
    }


def test_no_chat_fetches_top_sources_and_cites():
    pages = _pages()
    out = deep_research("roast beef", lambda q: _results(),
                        lambda url: pages[url],
                        chat=None, max_rounds=1, max_sources=2)
    assert [e["url"] for e in out["evidence"]] == [
        "https://a.example/x", "https://b.example/y"]
    assert out["citations"] == ["https://a.example/x",
                                "https://b.example/y"]
    assert "https://a.example/x" in out["summary"]
    assert out["rounds"] == 1


def test_judge_escalates_only_flagged_snippets():
    pages = _pages()
    seen = []

    def chat(messages):
        seen.append(messages)
        return '{"fetch": [1]}'
    out = deep_research("roast beef", lambda q: _results(),
                        lambda url: pages[url],
                        chat=chat, max_rounds=1, max_sources=3)
    assert [e["url"] for e in out["evidence"]] == ["https://b.example/y"]
    assert len(seen) == 2  # judge + synthesis


def test_judge_failure_falls_back_to_top_sources():
    pages = _pages()
    out = deep_research("roast beef", lambda q: _results(),
                        lambda url: pages[url],
                        chat=lambda m: "garbage, no json",
                        max_rounds=1, max_sources=1)
    assert [e["url"] for e in out["evidence"]] == ["https://a.example/x"]


def test_fetch_failure_skipped_not_fatal():
    def fetch(url):
        if "b.example" in url:
            raise RuntimeError("down")
        return _pages()[url]
    out = deep_research("roast beef", lambda q: _results(), fetch,
                        chat=None, max_rounds=1, max_sources=3)
    assert "https://b.example/y" not in out["citations"]
    assert "https://a.example/x" in out["citations"]


def test_thin_evidence_triggers_follow_up_round():
    pages = {"https://a.example/x": "roast beef\n\ncook it slowly"}
    calls = {"search": [], "chat": []}

    def search(query):
        calls["search"].append(query)
        if "follow" in query:
            return [{"title": "D", "url": "https://d.example/w",
                     "snippet": "more roast beef"}]
        return [{"title": "A", "url": "https://a.example/x",
                 "snippet": "roast beef"}]

    def fetch(url):
        return {"https://a.example/x": "roast beef\n\ncook it slowly",
                "https://d.example/w": "roast beef\n\nrest the beef"}[url]

    def chat(messages):
        calls["chat"].append(messages)
        text = messages[1].content
        if "follow-up" in messages[0].content:
            return '{"queries": ["follow roast beef"]}'
        return '{"fetch": [0]}'
    out = deep_research("roast beef", search, fetch, chat=chat,
                        max_rounds=2, max_sources=5)
    assert out["rounds"] == 2
    assert len(out["evidence"]) == 2
    assert "https://d.example/w" in out["citations"]
