"""Question-date authority: the date in the question wins.

A learned fact, a saved graph, or a research snippet may carry
the date it was learned on. When the user asks about a day,
that day in the question is the authority, not the date in
the fact. This module resolves the relative date words in a
question to a concrete date, so the prompt can say it plainly.

Rules:
- Matching is case-insensitive and word-boundary based, so
  "yesterday" does not match inside another word.
- "yesterday" wins over "today" when both appear in the same
  question; the other phrases are checked in a fixed order
  (yesterday, today, tomorrow, last week, next week,
  last month, last year) and the first match wins.
- "last year" is today minus 365 days. That is an
  approximation, not a calendar-year shift: across a leap
  day it lands one day off the same calendar date. It is
  documented here rather than hidden, because the prompt
  must show the resolved date, so a wrong one is visible.
- "last month" is a calendar-month shift, with the day
  clamped to the target month (31 March -> 28 February).
"""

from __future__ import annotations

import calendar
import datetime as _dt
import re

# Phrase -> resolver, in priority order. Multi-word phrases
# are matched as a whole, with word boundaries at both ends.
_PATTERNS = (
    ("yesterday", lambda today: today - _dt.timedelta(days=1)),
    ("today", lambda today: today),
    ("tomorrow", lambda today: today + _dt.timedelta(days=1)),
    ("last week", lambda today: today - _dt.timedelta(days=7)),
    ("next week", lambda today: today + _dt.timedelta(days=7)),
    ("last month", lambda today: _shift_month(today, -1)),
    ("last year", lambda today: today - _dt.timedelta(days=365)),
)


def _shift_month(today: _dt.date, months: int) -> _dt.date:
    """Shift a date by whole calendar months, clamping the day."""
    month_index = today.year * 12 + (today.month - 1) + months
    year, month0 = divmod(month_index, 12)
    month = month0 + 1
    day = min(today.day, calendar.monthrange(year, month)[1])
    return _dt.date(year, month, day)


def _as_date(today) -> _dt.date:
    """Accept a date, a datetime, or an ISO date string."""
    if today is None:
        return _dt.datetime.now(_dt.timezone.utc).date()
    if isinstance(today, _dt.datetime):
        return today.date()
    if isinstance(today, _dt.date):
        return today
    if isinstance(today, str):
        text = today.strip()
        try:
            return _dt.date.fromisoformat(text[:10])
        except ValueError:
            pass
        parsed = _dt.datetime.fromisoformat(text)
        return parsed.date()
    raise TypeError(f"today must be a date, datetime, or ISO string, not {type(today).__name__}")


def implied_date(question, today=None, timezone: str = "UTC"):
    """Return the date a question asks about, or None.

    ``today`` defaults to the current UTC date. ``timezone``
    is accepted for callers that track a local day; when
    ``today`` is given explicitly it already is the day,
    so the timezone does not shift it.
    """
    if not question or not str(question).strip():
        return None
    day = _as_date(today)
    text = str(question).lower()
    for phrase, resolve in _PATTERNS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", text):
            return resolve(day)
    return None


def question_date_note(question, today=None, timezone: str = "UTC") -> str:
    """A one-sentence note naming the asked-about date, or ""."""
    resolved = implied_date(question, today=today, timezone=timezone)
    if resolved is None:
        return ""
    return f"The question asks about {resolved.isoformat()}."
