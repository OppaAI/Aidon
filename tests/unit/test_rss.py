"""Native RSS: parsing (RSS + Atom) and new-item dedupe."""

from src.capabilities.rss import fetch_feed, parse_feed

_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Example</title>
<item><title>First post</title><link>https://example.com/1</link>
<guid>guid-1</guid><pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Second post</title><link>https://example.com/2</link>
<pubDate>Mon, 26 Oct 2026 11:00:00 GMT</pubDate></item>
</channel></rss>"""

_ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Example</title>
<entry><title>Atom post</title><link href="https://example.com/a"/>
<id>atom-1</id><updated>2026-10-26T12:00:00Z</updated></entry>
</feed>"""


def test_parse_rss_items():
    items = parse_feed(_RSS)
    assert len(items) == 2
    assert items[0]["id"] == "guid-1"
    assert items[0]["link"] == "https://example.com/1"
    assert items[0]["published"].startswith("Mon, 26 Oct")
    # No guid: the link is the id.
    assert items[1]["id"] == "https://example.com/2"


def test_parse_atom_entry():
    items = parse_feed(_ATOM)
    assert len(items) == 1
    assert items[0]["id"] == "atom-1"
    assert items[0]["link"] == "https://example.com/a"
    assert items[0]["published"] == "2026-10-26T12:00:00Z"


def test_parse_bad_xml_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_feed("<rss><unclosed>")


def test_fetch_feed_dedupes_across_calls(tmp_path):
    state = tmp_path / "seen.json"
    opener = lambda url: _RSS.encode()
    first = fetch_feed("https://example.com/feed", state, opener=opener)
    assert "First check" in first
    assert "First post" in first and "Second post" in first
    second = fetch_feed("https://example.com/feed", state, opener=opener)
    assert second == "No new items from https://example.com/feed."
    third_xml = _RSS.replace("</channel>",
                             "<item><title>Third</title>"
                             "<link>https://example.com/3</link>"
                             "<guid>guid-3</guid></item></channel>")
    third = fetch_feed("https://example.com/feed", state,
                       opener=lambda url: third_xml.encode())
    assert "1 new item" in third
    assert "Third" in third
    assert "First post" not in third


def test_fetch_feed_failure_is_text(tmp_path):
    def _boom(url):
        raise RuntimeError("network down")

    result = fetch_feed("https://example.com/feed", tmp_path / "seen.json",
                        opener=_boom)
    assert result.startswith("feed failed:")
