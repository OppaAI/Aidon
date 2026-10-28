import datetime

from src.agentic.question_dates import implied_date, question_date_note
from src.capabilities.webchat import (
    read_only_tools,
    webchat_reply,
    webchat_reply_with_local,
)

TODAY = datetime.date(2026, 10, 22)


class _RecordingClient:
    """A model client that records the prompt it was given."""

    def __init__(self):
        self.prompts = []

    def chat(self, messages):
        self.prompts.append(messages[0].content)
        return "answer"


def _researcher(facts):
    return lambda goal: list(facts)


# ---- implied_date ---------------------------------------------------------

def test_yesterday():
    assert implied_date("what did bitcoin close at yesterday?", TODAY) == datetime.date(2026, 10, 21)


def test_today():
    assert implied_date("the price today", TODAY) == datetime.date(2026, 10, 22)


def test_tomorrow():
    assert implied_date("the price tomorrow", TODAY) == datetime.date(2026, 10, 23)


def test_last_week():
    assert implied_date("the close last week", TODAY) == datetime.date(2026, 10, 15)


def test_last_month():
    assert implied_date("the close last month", TODAY) == datetime.date(2026, 9, 22)


def test_last_month_clamps_to_month_end():
    assert implied_date("the close last month", datetime.date(2026, 3, 31)) == datetime.date(2026, 2, 28)


def test_next_week():
    assert implied_date("what about next week?", TODAY) == datetime.date(2026, 10, 29)


def test_last_year_is_365_days():
    assert implied_date("the close last year", TODAY) == datetime.date(2025, 10, 22)


def test_yesterday_wins_over_today():
    assert implied_date("yesterday, not today, what was the close?", TODAY) == datetime.date(2026, 10, 21)


def test_case_insensitive_and_word_boundaries():
    assert implied_date("What was the close YESTERDAY?", TODAY) == datetime.date(2026, 10, 21)
    assert implied_date("what is the capital of Peru?", TODAY) is None


def test_no_date_returns_none():
    assert implied_date("what is the capital of Peru?", TODAY) is None
    assert implied_date("", TODAY) is None


def test_iso_string_today():
    assert implied_date("the price yesterday", "2026-10-22") == datetime.date(2026, 10, 21)


# ---- question_date_note ---------------------------------------------------

def test_question_date_note_wording():
    assert question_date_note("what was the close yesterday?", TODAY) == "The question asks about 2026-10-21."


def test_question_date_note_empty_without_date():
    assert question_date_note("what is the capital of Peru?", TODAY) == ""


# ---- webchat_reply_with_local ---------------------------------------------

def test_local_reply_includes_date_note_and_memory_rule(tmp_path):
    (tmp_path / "budget.md").write_text("Budget: 90 dollars.")
    client = _RecordingClient()
    reply = webchat_reply_with_local(
        "what is my budget yesterday? check budget.md",
        _researcher(["A learned fact dated 2026-10-19."]),
        client,
        read_file=lambda path: (tmp_path / path).read_text(),
        today=TODAY)
    assert reply == "answer"
    prompt = client.prompts[0]
    assert "The question asks about 2026-10-21." in prompt
    assert "never from conversation memory" in prompt
    assert "date in the question is the authority" in prompt
    assert "Budget: 90 dollars." in prompt


def test_local_reply_without_named_file_still_carries_rule():
    client = _RecordingClient()
    webchat_reply_with_local(
        "what is my budget yesterday?",
        _researcher(["A fact."]),
        client,
        read_file=lambda path: "unused",
        today=TODAY)
    prompt = client.prompts[0]
    assert "The question asks about 2026-10-21." in prompt
    assert "never from conversation memory" in prompt


def test_fallback_to_plain_webchat_without_read_file():
    client = _RecordingClient()
    plain = _RecordingClient()
    goal = "what is my budget yesterday?"
    webchat_reply_with_local(goal, _researcher(["A fact."]), client, read_file=None, today=TODAY)
    webchat_reply(goal, _researcher(["A fact."]), plain)
    assert client.prompts[0] == plain.prompts[0]


def test_fallback_to_plain_webchat_without_local_words():
    client = _RecordingClient()
    plain = _RecordingClient()
    goal = "what is the capital of Peru?"
    webchat_reply_with_local(goal, _researcher(["A fact."]), client,
                             read_file=lambda path: "unused", today=TODAY)
    webchat_reply(goal, _researcher(["A fact."]), plain)
    assert client.prompts[0] == plain.prompts[0]


def test_local_reply_with_no_facts_but_local_text_still_answers(tmp_path):
    (tmp_path / "notes.md").write_text("Notes: roast beef dinner.")
    client = _RecordingClient()
    reply = webchat_reply_with_local(
        "what do my notes.md say?",
        _researcher([]),
        client,
        read_file=lambda path: (tmp_path / path).read_text(),
        today=TODAY)
    assert reply == "answer"
    assert "roast beef dinner" in client.prompts[0]


# ---- read_only_tools ------------------------------------------------------

def test_read_only_tools_has_read_only(tmp_path):
    tools = read_only_tools(tmp_path)
    assert set(tools) == {"read"}
    (tmp_path / "a.md").write_text("hello")
    assert tools["read"]("a.md") == "hello"


def test_read_only_tools_refuses_paths_outside_workdir(tmp_path):
    tools = read_only_tools(tmp_path)
    for bad in ("../secret.txt", "/etc/passwd"):
        try:
            tools["read"](bad)
        except ValueError as exc:
            assert "outside workdir" in str(exc)
        else:
            raise AssertionError(f"{bad} should have been refused")
