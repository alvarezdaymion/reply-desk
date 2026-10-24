"""Follow-up timing from what a person wrote, against a fixed clock."""

import datetime as dt

import pytest

from reply_desk.timing import add_months, parse_when

WED = dt.datetime(2026, 10, 21, 14, 0, tzinfo=dt.UTC)  # Wednesday 21 October 2026, 14:00
SAT = dt.datetime(2026, 10, 24, 11, 0, tzinfo=dt.UTC)


def due(text, now=WED, **kw):
    hint = parse_when(text, now, **kw)
    return hint.due.strftime("%a %Y-%m-%d %H:%M") if hint else None


@pytest.mark.parametrize("text,expected", [
    ("I'll look it over this weekend.", "Mon 2026-10-26 09:00"),
    ("Will read it over the weekend", "Mon 2026-10-26 09:00"),
    ("Let's talk next week.", "Mon 2026-11-02 09:00"),
    ("Swamped until after the holidays.", "Mon 2027-01-04 09:00"),  # 2 January 2027 is a Saturday
    ("Can we pick this up in the new year?", "Mon 2027-01-04 09:00"),
    ("Check back in two weeks", "Wed 2026-11-04 09:00"),
    ("try me in 3 days", "Mon 2026-10-26 09:00"),  # lands on a Saturday, moves to Monday
    ("ping me in a couple of weeks", "Wed 2026-11-04 09:00"),
    ("I'll get back to you Friday", "Mon 2026-10-26 09:00"),  # the business day after Friday
    ("by Thursday at the latest", "Fri 2026-10-23 09:00"),
    ("Let me think about it tomorrow", "Fri 2026-10-23 09:00"),
    ("end of the week works", "Mon 2026-10-26 09:00"),
    ("Reach out next month", "Tue 2026-12-01 09:00"),
    ("in a month or so", "Mon 2026-11-23 09:00"),
])
def test_phrases(text, expected):
    assert due(text) == expected


def test_no_time_named_means_no_hint():
    assert due("Sounds good, send it over.") is None


def test_the_latest_time_named_wins():
    assert due("Maybe Friday, or more likely next week") == "Mon 2026-11-02 09:00"


def test_said_on_a_weekend():
    assert due("I'll read it this weekend", now=SAT) == "Mon 2026-10-26 09:00"
    assert due("Talk Friday", now=SAT) == "Mon 2026-11-02 09:00"  # next Friday, then the Monday after


def test_the_weekday_named_today_counts_as_today():
    friday = dt.datetime(2026, 10, 23, 8, 0, tzinfo=dt.UTC)
    assert due("I'll reply Friday", now=friday) == "Mon 2026-10-26 09:00"


def test_send_hour_and_time_zone_follow_the_clock():
    ny = dt.datetime(2026, 10, 21, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    hint = parse_when("next week", ny, send_hour=8)
    assert hint.due.isoformat() == "2026-11-02T08:00:00-04:00" and hint.kind == "other"


def test_far_off_dates_are_ignored_as_misreads():
    march = dt.datetime(2026, 3, 10, 9, 0, tzinfo=dt.UTC)
    assert due("after the holidays", now=march) is None
    assert due("after the holidays or next week", now=march) == "Mon 2026-03-23 09:00"


def test_hint_keeps_the_words_and_the_kind():
    hint = parse_when("We're slammed. Over the weekend I'll look.", WED)
    assert (hint.phrase, hint.kind) == ("Over the weekend", "weekend")
    assert parse_when("after the holidays", WED).kind == "holidays"


def test_add_months_clamps_to_the_last_day():
    assert add_months(dt.date(2026, 1, 31), 1) == dt.date(2026, 2, 28)
    assert add_months(dt.date(2026, 11, 15), 2) == dt.date(2027, 1, 15)
