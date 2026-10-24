"""Turn what a person wrote about timing into a follow-up date.

One rule: the follow-up goes out on the first business day after the time they named has passed,
at the configured send hour. "This weekend" means Monday.

| They wrote                                             | Follow-up goes out                         |
|--------------------------------------------------------|--------------------------------------------|
| "this weekend", "over the weekend"                     | the Monday after                           |
| "tomorrow"                                             | the first business day after tomorrow      |
| a weekday: "Friday", "by Thursday" (today counts)      | the first business day after that day      |
| "end of the week", "later this week"                   | the Monday after this week                 |
| "next week"                                            | the Monday after next week                 |
| "next month"                                           | the first business day after next month    |
| "after the holidays", "in the new year"                | the first business day after 1 January     |
| "in two weeks", "in 10 days", "in a couple of months"  | that day (a weekend moves to Monday)       |

When several phrases match, the latest date wins, so nothing goes out before every time they named
has passed. A date more than `max_days_out` ahead is treated as a misread and ignored. Business days
are Monday to Friday; public holidays are not modelled.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
           "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "a couple of": 2, "a couple": 2,
           "couple of": 2, "a few": 3, "few": 3}
_NUM = "|".join(sorted((re.escape(k) for k in NUMBERS), key=len, reverse=True))

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("weekend", re.compile(r"\b(?:this|over the|on the|the) weekend\b", re.I)),
    ("tomorrow", re.compile(r"\btomorrow\b", re.I)),
    ("end_of_week", re.compile(r"\b(?:end of (?:the |this )?week|later this week)\b", re.I)),
    ("next_week", re.compile(r"\bnext week\b", re.I)),
    ("next_month", re.compile(r"\bnext month\b", re.I)),
    ("holidays", re.compile(r"\b(?:after|past) the (?:holidays|new year)\b|\b(?:in|after) the new year\b"
                            r"|\bafter new year'?s\b", re.I)),
    ("in_n", re.compile(r"\bin (\d{1,3}|%s) (day|week|month)s?\b" % _NUM, re.I)),
    ("weekday", re.compile(r"\b(%s)\b" % "|".join(WEEKDAYS), re.I)),
]


@dataclass(frozen=True)
class Hint:
    due: dt.datetime   # aware, in the time zone of `now`
    phrase: str        # the words that set the date, as written
    kind: str          # "weekend", "holidays" or "other": picks the follow-up opener


def next_business_day(day: dt.date) -> dt.date:
    """`day` itself if it is Monday to Friday, else the Monday after."""
    while day.weekday() >= 5:
        day += dt.timedelta(days=1)
    return day


def business_day_after(day: dt.date) -> dt.date:
    return next_business_day(day + dt.timedelta(days=1))


def monday_after_this_week(today: dt.date) -> dt.date:
    return today + dt.timedelta(days=7 - today.weekday())


def add_months(day: dt.date, months: int) -> dt.date:
    year, month = divmod(day.month - 1 + months, 12)
    year, month = day.year + year, month + 1
    last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day.day, last))


def _day_for(kind: str, m: re.Match[str], today: dt.date) -> dt.date | None:
    if kind in ("weekend", "end_of_week"):
        return monday_after_this_week(today)
    if kind == "tomorrow":
        return business_day_after(today + dt.timedelta(days=1))
    if kind == "next_week":
        return monday_after_this_week(today) + dt.timedelta(days=7)
    if kind == "next_month":
        return next_business_day(add_months(today.replace(day=1), 2))
    if kind == "holidays":
        new_year = today if (today.month, today.day) == (1, 1) else dt.date(today.year + 1, 1, 1)
        return business_day_after(new_year)
    if kind == "in_n":
        raw, unit = m.group(1).lower(), m.group(2).lower()
        n = int(raw) if raw.isdigit() else NUMBERS[raw]
        if n < 1:
            return None
        if unit == "month":
            return next_business_day(add_months(today, n))
        return next_business_day(today + dt.timedelta(days=n * (7 if unit == "week" else 1)))
    if kind == "weekday":
        target = WEEKDAYS.index(m.group(1).lower())
        return business_day_after(today + dt.timedelta(days=(target - today.weekday()) % 7))
    return None


def parse_when(text: str, now: dt.datetime, *, send_hour: int = 9, max_days_out: int = 90) -> Hint | None:
    """The follow-up time implied by `text`, or None when it names no time we understand."""
    today = now.date()
    found: list[tuple[dt.date, str, str]] = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(text or ""):
            day = _day_for(kind, m, today)
            if day is not None and 0 < (day - today).days <= max_days_out:
                found.append((day, m.group(0), kind))
    if not found:
        return None
    day, phrase, kind = max(found, key=lambda f: f[0])
    due = dt.datetime.combine(day, dt.time(send_hour), tzinfo=now.tzinfo)
    return Hint(due=due, phrase=phrase, kind=kind if kind in ("weekend", "holidays") else "other")


def human_day(when: dt.datetime) -> str:
    """'Monday 26 October', with no platform-specific strftime flags."""
    return "%s %d %s" % (when.strftime("%A"), when.day, when.strftime("%B"))
