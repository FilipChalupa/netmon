"""Convert range=day|week|all (+ date) into a (t0, t1) interval in epoch seconds."""

from __future__ import annotations

import datetime
import time
from zoneinfo import ZoneInfo


# day-of-month without leading zero, portably: strftime's %-d is a glibc
# extension and raises "Invalid format string" on Windows
def _md(d) -> str:
    return f"{d:%b} {d.day}"


def _mdy(d) -> str:
    return f"{_md(d)}, {d.year}"


def resolve_range(range_: str, date_: str | None, tz_name: str) -> tuple[float, float, str]:
    """Returns (t0, t1, period label). date_ = YYYY-MM-DD (day/week only)."""
    tz = ZoneInfo(tz_name)
    now = datetime.datetime.now(tz)

    if range_ == "all":
        return 0.0, time.time(), "entire measurement"

    if range_ in ("24h", "48h"):
        hours = int(range_[:-1])
        end = time.time()
        return end - hours * 3600, end, f"last {hours} h"

    if date_:
        day = datetime.date.fromisoformat(date_)
    else:
        day = now.date()

    if range_ == "week":
        start = datetime.datetime.combine(day - datetime.timedelta(days=6),
                                          datetime.time.min, tz)
        end = datetime.datetime.combine(day + datetime.timedelta(days=1),
                                        datetime.time.min, tz)
        label = f"{_md(start)} – {_mdy(day)}"
        return start.timestamp(), min(end.timestamp(), time.time()), label

    # day (default)
    start = datetime.datetime.combine(day, datetime.time.min, tz)
    end = start + datetime.timedelta(days=1)
    return start.timestamp(), min(end.timestamp(), time.time()), _mdy(day)


def _parse_point(s: str) -> tuple[datetime.datetime, bool]:
    """ISO date or datetime string → (naive datetime, was_date_only)."""
    if len(s) == 10:
        return (datetime.datetime.combine(datetime.date.fromisoformat(s),
                                          datetime.time.min), True)
    return datetime.datetime.fromisoformat(s), False


def custom_ctx(from_: str | None, to_: str | None, tz_name: str) -> dict:
    """range=custom template context from from/to query params.

    Accepts YYYY-MM-DD (inclusive days, the date picker) or
    YYYY-MM-DDTHH:MM (exact span, drag-to-zoom links). Swapped bounds are
    normalized; malformed input raises ValueError. The ‹ › links page by
    the range's own length and keep its granularity.
    """
    tz = ZoneInfo(tz_name)
    p0, only0 = _parse_point(from_ or "")
    p1, only1 = _parse_point(to_ or from_ or "")
    if p1 < p0:
        (p0, only0), (p1, only1) = (p1, only1), (p0, only0)
    dates_only = only0 and only1
    end = p1 + datetime.timedelta(days=1) if only1 else p1
    if end <= p0:
        raise ValueError("empty range")
    t0 = p0.replace(tzinfo=tz).timestamp()
    t1 = end.replace(tzinfo=tz).timestamp()
    now = time.time()

    if dates_only:
        label = (_mdy(p0) if p0.date() == p1.date()
                 else f"{_md(p0)} – {_mdy(p1)}")
        fmt = lambda dt: dt.date().isoformat()  # noqa: E731
    elif p0.date() == end.date():
        label = f"{_mdy(p0)} · {p0:%H:%M} – {end:%H:%M}"
        fmt = lambda dt: dt.isoformat(timespec="minutes")  # noqa: E731
    else:
        label = f"{_md(p0)} {p0:%H:%M} – {_md(end)} {end:%H:%M}, {end.year}"
        fmt = lambda dt: dt.isoformat(timespec="minutes")  # noqa: E731

    span = end - p0
    has_next = t1 < now - 60
    return {
        "range": "custom",
        "t0": t0,
        "t1": min(t1, now),
        "range_label": label,
        # the date inputs are day-granular even when zoomed below a day
        "from_date": p0.date().isoformat(),
        "to_date": (p1 if only1 else end).date().isoformat(),
        "prev_from": fmt(p0 - span),
        "prev_to": fmt(p1 - span),
        "next_from": fmt(p0 + span) if has_next else None,
        "next_to": fmt(p1 + span) if has_next else None,
        "is_today": not has_next,
    }


def day_bounds(day: datetime.date, tz_name: str) -> tuple[float, float]:
    tz = ZoneInfo(tz_name)
    start = datetime.datetime.combine(day, datetime.time.min, tz)
    return start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()
