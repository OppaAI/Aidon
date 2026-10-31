from types import SimpleNamespace


def test_web_researcher_keeps_source_urls_for_lab_citations(monkeypatch, tmp_path):
    from src.capabilities import search
    from src.system.main import _make_web_researcher

    monkeypatch.setattr(search, "search", lambda *args, **kwargs: {
        "results": [{"title": "Official guide", "snippet": "Supported export formats.",
                     "url": "https://example.test/guide"},
                    {"title": "Missing source", "snippet": "No URL"}],
    })
    config = SimpleNamespace(search_backend="test", tavily_api_key="",
                             searxng_url="", home=tmp_path,
                             research_allowlist="")
    evidence = _make_web_researcher(config)("compare video editors")
    assert evidence == [
        "Official guide: Supported export formats. (https://example.test/guide)",
    ]
