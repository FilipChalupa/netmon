"""Time ranges: custom from/to bounds for the date range picker."""

import datetime
import time
from zoneinfo import ZoneInfo

from netmon_server.timerange import custom_bounds, resolve_range

TZ = "Europe/Prague"


def _local(day, hour=0):
    return datetime.datetime.combine(day, datetime.time(hour),
                                     ZoneInfo(TZ)).timestamp()


def test_custom_bounds_inclusive_days():
    d0 = datetime.date(2026, 7, 1)
    d1 = datetime.date(2026, 7, 14)
    t0, t1, label = custom_bounds(d0, d1, TZ)
    assert t0 == _local(d0)
    assert t1 == _local(d1 + datetime.timedelta(days=1))
    assert label == "Jul 1 – Jul 14, 2026"


def test_custom_bounds_single_day():
    d = datetime.date(2026, 7, 1)
    t0, t1, label = custom_bounds(d, d, TZ)
    assert t1 - t0 == 24 * 3600
    assert label == "Jul 1, 2026"


def test_custom_bounds_caps_at_now():
    today = datetime.datetime.now(ZoneInfo(TZ)).date()
    _, t1, _ = custom_bounds(today - datetime.timedelta(days=1), today, TZ)
    assert t1 <= time.time() + 1


def test_custom_bounds_spans_dst_change():
    # CET → CEST on 2026-03-29: the range is 2 h shorter than naive 3 days
    t0, t1, _ = custom_bounds(datetime.date(2026, 3, 28),
                              datetime.date(2026, 3, 30), TZ)
    assert t1 - t0 == 3 * 24 * 3600 - 3600


def test_resolve_range_day_unchanged():
    t0, t1, _ = resolve_range("day", "2026-07-01", TZ)
    assert t1 - t0 == 24 * 3600
