"""Extra plan tools exposed through _goal_extra_tools.

No real network is hit: messaging senders are tested only in their
unconfigured state (tokens removed from the environment), fetch_url
is tested only on a clearly invalid input, and document/cache tests
use temp files and a temp home.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.agentic.research_cache import ResearchCache
from src.interface.triggers import Scheduler, SchedulerJob
from src.system.main import _goal_extra_tools


class _Config:
    github_token = ""
    threads_account_id = ""
    threads_access_token = ""
    ig_user_id = ""
    ig_access_token = ""
    fish_api_key = ""
    tts_voice_id = ""
    transcribe_api_key = ""
    owner_email = "owner@example.com"


def _tools(tmp_path, **kw):
    cfg = _Config()
    cfg.home = tmp_path
    return _goal_extra_tools(cfg, tmp_path / "work", **kw)


EXPECTED = ["web_search", "fetch_url", "read_document", "search_document",
            "search_research_cache", "slack_send", "whatsapp_send",
            "list_schedule", "cancel_schedule"]


def test_all_nine_tools_present(tmp_path):
    tools = _tools(tmp_path)
    for name in EXPECTED:
        assert name in tools, name
        assert callable(tools[name])


def test_schedule_list_and_cancel_round_trip(tmp_path):
    scheduler = Scheduler(tmp_path)
    scheduler.add_job(SchedulerJob(name="morning", schedule="0 9 * * *",
                                   goal="check mail"))
    tools = _tools(tmp_path)
    listing = tools["list_schedule"]()
    assert "morning" in listing and "check mail" in listing
    assert "cancelled morning" in tools["cancel_schedule"]("morning")
    assert "morning" not in tools["list_schedule"]()
    assert "no job named morning" in tools["cancel_schedule"]("morning")


def test_web_search_without_researcher_is_plain_string(tmp_path):
    tools = _tools(tmp_path)
    result = tools["web_search"]("dinner prices")
    assert isinstance(result, str)
    assert "not configured" in result


def test_web_search_with_researcher_joins_results(tmp_path):
    tools = _tools(tmp_path, researcher=lambda q: [f"hit one for {q}", "hit two"])
    result = tools["web_search"]("dinner prices")
    assert "hit one for dinner prices" in result
    assert "hit two" in result


def test_fetch_url_invalid_input_returns_string_without_raising(tmp_path):
    tools = _tools(tmp_path)
    result = tools["fetch_url"]("not a url at all")
    assert isinstance(result, str) and result


def test_read_document_local_temp_file(tmp_path):
    doc = tmp_path / "note.txt"
    doc.write_text("the quick brown fox jumps over the lazy dog", encoding="utf-8")
    tools = _tools(tmp_path)
    result = tools["read_document"](str(doc))
    assert isinstance(result, str)
    assert "quick brown fox" in result


def test_search_document_empty_home_returns_string(tmp_path):
    tools = _tools(tmp_path)
    result = tools["search_document"]("something never ingested")
    assert isinstance(result, str)
    assert "no documents matched" in result


def test_search_research_cache_round_trip(tmp_path):
    cache = ResearchCache(tmp_path)
    cache.store("https://example.com/beef", "roast beef prices in Vancouver")
    tools = _tools(tmp_path)
    result = tools["search_research_cache"]("roast beef")
    assert "example.com/beef" in result
    assert "no cached research matched" in tools["search_research_cache"]("zzz-no-match")


def test_messaging_sends_unconfigured_return_strings(tmp_path, monkeypatch):
    for var in ("AIDON_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN",
                "AIDON_WHATSAPP_TOKEN", "WHATSAPP_TOKEN",
                "AIDON_WHATSAPP_PHONE_ID"):
        monkeypatch.delenv(var, raising=False)
    tools = _tools(tmp_path)
    slack = tools["slack_send"]("#general", "hello")
    assert isinstance(slack, str) and "failed" in slack
    wa = tools["whatsapp_send"]("+1234567890", "hello")
    assert isinstance(wa, str) and "failed" in wa
