"""Scheduler cron: the CLI default must fire, month and weekday must count.

Regression for a silent failure: `schedule add` defaults to
``0 9 * * *``, but the old `next_due` only accepted ``cron M H DOM
MON DOW`` (6 tokens, literal "cron"), so the default job never fired.
The old parser also ignored the month field and used Python's
Monday=0 weekday where cron uses Sunday=0.
"""

from datetime import datetime

from src.interface.triggers import SchedulerJob


def _job(schedule: str) -> SchedulerJob:
    return SchedulerJob(name="j", schedule=schedule, goal="g")


def test_cli_default_schedule_fires_daily():
    after = datetime(2026, 10, 26, 10, 0)
    assert _job("0 9 * * *").next_due(after) == datetime(2026, 10, 27, 9, 0)
    # Before today's run, it is today.
    assert _job("0 9 * * *").next_due(
        datetime(2026, 10, 26, 8, 0)) == datetime(2026, 10, 26, 9, 0)


def test_legacy_cron_prefix_still_works():
    after = datetime(2026, 10, 26, 10, 0)
    assert _job("cron 0 9 * * *").next_due(after) == datetime(
        2026, 10, 27, 9, 0)


def test_month_is_not_ignored():
    after = datetime(2026, 10, 26, 10, 0)
    # June only: next June 1, not November 1.
    assert _job("0 0 1 6 *").next_due(after) == datetime(2027, 6, 1, 0, 0)


def test_leap_day_skips_non_leap_years():
    # February 29 is a valid yearly schedule even when the next year is
    # not leap; it should advance to the next real occurrence.
    assert _job("0 9 29 2 *").next_due(
        datetime(2025, 3, 1, 0, 0)) == datetime(2028, 2, 29, 9, 0)
    assert _job("0 9 29 2 *").next_due(
        datetime(2028, 2, 29, 10, 0)) == datetime(2032, 2, 29, 9, 0)


def test_weekday_uses_cron_sunday_zero():
    after = datetime(2026, 10, 26, 10, 0)  # Monday
    assert _job("0 9 * * 0").next_due(after) == datetime(2026, 11, 1, 9, 0)
    assert _job("0 9 * * 1").next_due(after) == datetime(2026, 11, 2, 9, 0)


def test_ranges_lists_and_steps():
    after = datetime(2026, 10, 26, 10, 7)
    assert _job("*/15 * * * *").next_due(after) == datetime(
        2026, 10, 26, 10, 15)
    assert _job("0 9 * * 1-5").next_due(after) == datetime(
        2026, 10, 27, 9, 0)
    assert _job("0 9,17 * * *").next_due(after) == datetime(
        2026, 10, 26, 17, 0)


def test_one_shot_at_does_not_repeat():
    after = datetime(2026, 10, 26, 10, 0)
    assert _job("at:2026-11-01T09:00:00").next_due(after) == datetime(
        2026, 11, 1, 9, 0)
    assert _job("at:2026-10-01T09:00:00").next_due(after) is None


def test_invalid_schedule_is_none_not_a_crash():
    after = datetime(2026, 10, 26, 10, 0)
    assert _job("bogus").next_due(after) is None
    assert _job("99 9 * * *").next_due(after) is None
