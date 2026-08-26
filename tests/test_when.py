"""Turning "last night" into a window.

The failure this prevents is not an exception — it is a confident answer about
the wrong evening. Every case pins `now` explicitly, because a window helper
tested against the real clock is a test that passes at noon and fails at 00:30.
"""

from __future__ import annotations

import datetime as dt

from app import when

#: Wednesday 12 August 2026, mid-afternoon. Same instant for every case below.
NOW = dt.datetime(2026, 8, 12, 15, 30)


def _window(phrase, now=NOW):
    since, until, label = when.parse(phrase, now=now)
    return (dt.datetime.fromtimestamp(since), dt.datetime.fromtimestamp(until), label)


def test_last_night_starts_yesterday_evening_and_runs_into_this_morning():
    """The bug worth naming: reading 'night' as 'yesterday's date' drops 00:40."""
    start, end, label = _window("what did vinish say last night")
    assert start == dt.datetime(2026, 8, 11, 18, 0)
    assert end == dt.datetime(2026, 8, 12, 6, 0)
    assert label == "last night"


def test_a_message_sent_after_midnight_falls_inside_last_night():
    since, until, _ = when.parse("last night", now=NOW)
    after_midnight = dt.datetime(2026, 8, 12, 0, 40).timestamp()
    assert since <= after_midnight <= until


def test_asking_at_2am_treats_the_evening_as_still_running():
    """At 02:00 'last night' has not finished, so the window ends now."""
    now = dt.datetime(2026, 8, 12, 2, 0)
    start, end, _ = _window("last night", now=now)
    assert start == dt.datetime(2026, 8, 11, 18, 0)
    assert end == now, "window ran into a 06:00 that has not happened yet"


def test_yesterday_is_the_whole_calendar_day():
    start, end, label = _window("anything from suraj yesterday")
    assert start == dt.datetime(2026, 8, 11, 0, 0)
    assert end == dt.datetime(2026, 8, 12, 0, 0)
    assert label == "yesterday"


def test_today_runs_to_now_not_to_midnight():
    start, end, label = _window("messages today")
    assert start == dt.datetime(2026, 8, 12, 0, 0)
    assert end == NOW
    assert label == "today"


def test_this_morning_stops_at_noon():
    start, end, _ = _window("what came in this morning")
    assert start == dt.datetime(2026, 8, 12, 0, 0)
    assert end == dt.datetime(2026, 8, 12, 12, 0)


def test_an_explicit_number_beats_a_named_window():
    """Someone who typed a number meant it."""
    start, end, label = _window("last 3 hours")
    assert end - start == dt.timedelta(hours=3)
    assert "3" in label


def test_explicit_minutes_and_days_both_parse():
    s1, e1, _ = when.parse("past 30 minutes", now=NOW)
    assert round(e1 - s1) == 1800
    s2, e2, _ = when.parse("since 2 days", now=NOW)
    assert round(e2 - s2) == 2 * 86400


def test_last_week_covers_seven_whole_days_up_to_now():
    """Day-granularity on purpose: a week-scale question means whole days, so the
    window opens at midnight seven days back rather than at this time of day."""
    start, end, label = _window("what did they say last week")
    assert start == dt.datetime(2026, 8, 5, 0, 0)
    assert end == NOW
    assert "7 days" in label


def test_unrecognised_input_falls_back_rather_than_raising():
    """A too-wide window still answers; an exception answers nothing."""
    start, end, label = _window("sometime around the thing")
    assert end - start == dt.timedelta(hours=when.DEFAULT_HOURS)
    assert "24h" in label


def test_while_i_was_away_uses_the_real_sleep_gap(monkeypatch):
    """The window is the actual suspend, not a guess at one."""
    slept_at = dt.datetime(2026, 8, 12, 7, 0).timestamp()
    monkeypatch.setattr(when, "__name__", when.__name__)  # no-op, keeps import local
    from app import wake
    monkeypatch.setattr(wake, "last_gap", lambda: (slept_at, 9 * 3600))

    since, until, label = when.parse("what did I miss while I was away", now=NOW)
    assert since == slept_at - 9 * 3600
    assert label == "while the laptop was asleep"


def test_while_i_was_away_falls_back_when_nothing_ever_slept(monkeypatch):
    from app import wake
    monkeypatch.setattr(wake, "last_gap", lambda: (0.0, 0.0))
    since, until, label = when.parse("anything I missed", now=NOW)
    assert "24h" in label


def test_describe_shows_the_window_it_actually_used():
    since, until, _ = when.parse("last night", now=NOW)
    text = when.describe(since, until)
    assert "11 Aug 18:00" in text and "12 Aug 06:00" in text
