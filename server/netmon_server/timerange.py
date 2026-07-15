"""Převod range=day|week|all (+ date) na interval (t0, t1) v epoch sekundách."""

from __future__ import annotations

import datetime
import time
from zoneinfo import ZoneInfo


def resolve_range(range_: str, date_: str | None, tz_name: str) -> tuple[float, float, str]:
    """Vrací (t0, t1, popisek období). date_ = YYYY-MM-DD (jen pro day/week)."""
    tz = ZoneInfo(tz_name)
    now = datetime.datetime.now(tz)

    if range_ == "all":
        return 0.0, time.time(), "celé měření"

    if date_:
        day = datetime.date.fromisoformat(date_)
    else:
        day = now.date()

    if range_ == "week":
        start = datetime.datetime.combine(day - datetime.timedelta(days=6),
                                          datetime.time.min, tz)
        end = datetime.datetime.combine(day + datetime.timedelta(days=1),
                                        datetime.time.min, tz)
        label = f"{start:%-d. %-m.} – {day:%-d. %-m. %Y}"
        return start.timestamp(), min(end.timestamp(), time.time()), label

    # day (výchozí)
    start = datetime.datetime.combine(day, datetime.time.min, tz)
    end = start + datetime.timedelta(days=1)
    return start.timestamp(), min(end.timestamp(), time.time()), f"{day:%-d. %-m. %Y}"


def day_bounds(day: datetime.date, tz_name: str) -> tuple[float, float]:
    tz = ZoneInfo(tz_name)
    start = datetime.datetime.combine(day, datetime.time.min, tz)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()
