"""Sixth-audit part E: YouTube learn, HTML stripping, Slack images, report tiers.

Covers the combined youtube_learn contract, the shared strip_html
contract on both the KB and research paths, Slack image intake, and
per-tier router counts surfacing in autonomy_report.
"""
import json

from src.agentic.autonomy_metrics import (
    ROUTER_FAST_CALL,
    ROUTER_STRONG_CALL,
    AutonomyMetrics,
)
from src.agentic.research import Researcher, _strip_html
from src.interface.report import autonomy_report
from src.interface.slack_inbound import parse_slack_event
from src.memory.kb import extract_text, strip_html
from src.social.youtube import youtube_learn


# -- YouTube learn -----------------------------------------------------

def _fake_search(url):
    assert "youtube/v3/search" in url
    return {"items": [{"id": {"videoId": "vid1"},
                       "snippet": {"title": "Roast Beef",
                                   "channelTitle": "Kitchen"}}]}


_XML = ('<transcript><text start="0" dur="1">Hello</text>'
        '<text start="1" dur="1">world</text></transcript>')


def test_youtube_learn_query_combines_metadata_and_transcript():
    result = youtube_learn(query="roast beef", api_key="k",
                           get=_fake_search, fetch=lambda url: _XML)
    assert result["ok"] is True
    assert result["metadata"]["video_id"] == "vid1"
    assert result["metadata"]["title"] == "Roast Beef"
    assert result["transcript"] == "Hello world"
    assert result["chapters"] == []


def test_youtube_learn_no_transcript_says_so_plainly():
    result = youtube_learn(video_id="vid9", fetch=lambda url: "")
    assert result["ok"] is True
    assert result["transcript"] is None
    assert result["transcript_available"] is False
    assert "No transcript available" in result["transcript_note"]
    assert result["metadata"]["video_id"] == "vid9"


def test_youtube_learn_visual_request_refused_not_faked():
    result = youtube_learn(query="x", api_key="k", visual_request=True)
    assert result["ok"] is False
    assert "text-only" in result["refusal"]
    assert result["transcript"] is None


def test_youtube_learn_needs_query_or_video_id():
    result = youtube_learn()
    assert result["ok"] is False
    assert result["transcript"] is None


# -- HTML stripping, shared --------------------------------------------

_HTML = """<html><head><style>.x{color:red}</style>
<script>var evil = 1;</script>
<script type="application/ld+json">{"name": "Roast Beef", "price": "12"}</script>
</head><body><p>Intro text</p>
<table><tr><th>Item</th><th>Price</th></tr>
<tr><td>Beef</td><td>$12</td></tr>
<tr><td>Potatoes</td><td>$3</td></tr></table>
</body></html>"""


def test_strip_html_removes_script_style_lifts_jsonld_keeps_rows():
    text = strip_html(_HTML)
    assert "evil" not in text
    assert "color:red" not in text
    assert "Roast Beef" in text  # lifted from JSON-LD
    assert "Intro text" in text
    lines = text.splitlines()
    assert "Item Price" in lines
    assert "Beef $12" in lines
    assert "Potatoes $3" in lines


def test_kb_and_research_share_the_same_stripper(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(_HTML, encoding="utf-8")
    assert extract_text(html_file) == strip_html(_HTML)
    assert _strip_html(_HTML) == strip_html(_HTML)


class _Cache:
    def __init__(self, raw):
        self.raw = raw

    def fetch(self, url):
        return self.raw


def test_research_excerpt_keeps_rows_and_claims_still_match():
    researcher = Researcher(cache=_Cache(_HTML))
    source = researcher.fetch("http://example.test/page")
    assert source.error == ""
    assert "Beef $12" in source.excerpt.splitlines()
    report = researcher.research("price?", ["http://example.test/page"],
                                 claims=["beef $12"])
    assert report.verified_facts, "claim split across lines must still verify"


# -- Slack image intake -------------------------------------------------

def _slack_payload(text="", files=None, file_shares=None):
    event = {"type": "message", "channel": "C1", "user": "U1", "text": text}
    if files is not None:
        event["files"] = files
    if file_shares is not None:
        event["file_shares"] = file_shares
    return {"event": event}


def test_slack_image_attached_like_whatsapp_marker():
    payload = _slack_payload(
        "what is this?", files=[
            {"id": "F1", "name": "plant.jpg", "mimetype": "image/jpeg",
             "url_private": "https://files.slack.test/F1"},
            {"id": "F2", "name": "notes.txt", "mimetype": "text/plain",
             "url_private": "https://files.slack.test/F2"},
        ])
    event = parse_slack_event(payload)
    assert event is not None
    assert "[image slack_file_id=F1" in event.goal
    assert "F2" not in event.goal  # non-image files are not attached
    assert len(event.payload["images"]) == 1
    assert event.payload["images"][0]["name"] == "plant.jpg"
    assert event.payload["images"][0]["downloaded"] is False


def test_slack_image_only_message_still_becomes_goal():
    payload = _slack_payload(file_shares=[
        {"id": "F3", "name": "pic.png", "mimetype": "image/png",
         "url_private": "https://files.slack.test/F3"}])
    event = parse_slack_event(payload)
    assert event is not None
    assert "slack_file_id=F3" in event.goal


def test_slack_text_only_unchanged_and_bot_ignored():
    event = parse_slack_event(_slack_payload("hello"))
    assert event is not None and event.goal == "hello"
    assert event.payload["images"] == []
    bot = {"event": {"type": "message", "bot_id": "B1", "text": "hi"}}
    assert parse_slack_event(bot) is None


# -- Report per-tier counts --------------------------------------------

def test_autonomy_report_surfaces_router_tiers(tmp_path):
    metrics = AutonomyMetrics(tmp_path)
    metrics.record(ROUTER_FAST_CALL, tokens=10)
    metrics.record(ROUTER_FAST_CALL, tokens=20)
    metrics.record(ROUTER_STRONG_CALL, tokens=100)
    report = autonomy_report(tmp_path)
    assert report["router_fast_calls"] == 2
    assert report["router_fast_tokens"] == 30
    assert report["router_strong_calls"] == 1
    assert report["router_strong_tokens"] == 100


def test_autonomy_report_without_metrics_file_is_zero(tmp_path):
    report = autonomy_report(tmp_path)
    assert report["router_fast_calls"] == 0
    assert report["router_strong_calls"] == 0
    assert report["router_fast_tokens"] == 0
    assert report["router_strong_tokens"] == 0
