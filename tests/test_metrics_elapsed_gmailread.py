"""Metrics truth grading, elapsed excluding approval wait, Gmail inbox read."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.elapsed import ElapsedTimer
from src.gmail_read import read_inbox
from src.metrics import grade, grade_from_directory


def test_grade_passes_fails_and_score():
    result = grade(
        {"menu.md": "Roast beef dinner for two", "ledger.md": "Total 95 dollars"},
        ["roast beef", "95 dollars", "turkey"],
    )
    assert result["passed"] == 2
    assert result["total"] == 3
    assert result["score"] == 2 / 3
    assert result["found"] == {"roast beef": True, "95 dollars": True, "turkey": False}


def test_grade_case_insensitive_and_empty_criteria():
    result = grade({"a.md": "HELLO World"}, ["hello"])
    assert result["found"]["hello"] is True
    empty = grade({"a.md": "anything"}, [])
    assert empty == {"passed": 0, "total": 0, "score": 0.0, "found": {}, "details": []}


def test_grade_from_directory(tmp_path):
    (tmp_path / "report.md").write_text("Roast beef, no milk, no eggs")
    result = grade_from_directory(tmp_path, ["roast beef", "milk", "peanuts"])
    assert result["passed"] == 2
    assert result["found"]["peanuts"] is False
    try:
        grade_from_directory(tmp_path / "missing", ["x"])
        assert False, "missing directory must raise"
    except ValueError:
        pass


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_timer_excludes_approval_wait():
    clock = FakeClock()
    timer = ElapsedTimer(time_fn=clock).start()
    clock.advance(10)
    with timer.approval_wait():
        clock.advance(30)
    clock.advance(5)
    assert timer.approval_wait_seconds() == 30
    assert timer.elapsed_total() == 45
    assert timer.elapsed_active() == 15


def test_timer_note_methods_and_nested_wait():
    clock = FakeClock()
    timer = ElapsedTimer(time_fn=clock).start()
    timer.note_approval_start()
    clock.advance(20)
    timer.note_approval_start()
    clock.advance(10)
    timer.note_approval_end()
    assert timer.approval_wait_seconds() == 30
    timer.note_approval_end()
    assert timer.approval_wait_seconds() == 30
    assert timer.elapsed_active() == 0


def _fake_post(url, payload):
    assert "oauth2.googleapis.com/token" in url
    assert payload["grant_type"] == "refresh_token"
    return {"access_token": "test-access"}


def _fake_get(url, headers):
    assert headers == {"Authorization": "Bearer test-access"}
    if "maxResults" in url:
        return {"messages": [{"id": "msg1"}, {"id": "msg2"}]}
    if url.endswith("/msg1?format=metadata"):
        return {
            "id": "msg1",
            "snippet": "First snippet",
            "payload": {"headers": [
                {"name": "Subject", "value": "Dinner plan"},
                {"name": "From", "value": "a@example.com"},
                {"name": "Date", "value": "Mon, 26 Oct 2026"},
            ]},
        }
    return {
        "id": "msg2",
        "snippet": "Second snippet",
        "payload": {"headers": [
            {"name": "Subject", "value": "Receipt"},
            {"name": "From", "value": "b@example.com"},
            {"name": "Date", "value": "Tue, 27 Oct 2026"},
        ]},
    }


def test_gmail_read_with_fake_post_get():
    messages = read_inbox("cid", "csecret", "refresh", post=_fake_post, get=_fake_get)
    assert len(messages) == 2
    assert messages[0]["subject"] == "Dinner plan"
    assert messages[0]["sender"] == "a@example.com"
    assert messages[0]["snippet"] == "First snippet"
    assert messages[0]["date"] == "Mon, 26 Oct 2026"
    assert messages[1]["subject"] == "Receipt"


def test_gmail_read_missing_token_raises():
    try:
        read_inbox("cid", "csecret", "", post=_fake_post, get=_fake_get)
        assert False, "missing refresh token must raise"
    except ValueError:
        pass
    try:
        read_inbox("cid", "csecret", "refresh", post=lambda url, payload: {}, get=_fake_get)
        assert False, "empty token response must raise"
    except ValueError:
        pass


def test_gmail_read_empty_list_response_raises():
    try:
        read_inbox("cid", "csecret", "refresh", post=_fake_post, get=lambda url, headers: None)
        assert False, "empty list response must raise"
    except ValueError:
        pass


def test_gmail_read_empty_inbox_returns_empty_list():
    messages = read_inbox(
        "cid", "csecret", "refresh", post=_fake_post,
        get=lambda url, headers: {"messages": []},
    )
    assert messages == []
