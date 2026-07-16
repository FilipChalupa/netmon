"""Time ranges: custom from/to bounds for the date range picker and zoom."""

import datetime
import time
from zoneinfo import ZoneInfo

import pytest

from netmon_server.timerange import custom_ctx, resolve_range

TZ = "Europe/Prague"


def _local(day, hour=0, minute=0):
    return datetime.datetime.combine(day, datetime.time(hour, minute),
                                     ZoneInfo(TZ)).timestamp()


def test_custom_inclusive_days():
    ctx = custom_ctx("2026-07-01", "2026-07-14", TZ)
    assert ctx["t0"] == _local(datetime.date(2026, 7, 1))
    assert ctx["t1"] == _local(datetime.date(2026, 7, 15))
    assert ctx["range_label"] == "Jul 1 – Jul 14, 2026"
    assert ctx["from_date"] == "2026-07-01"
    assert ctx["to_date"] == "2026-07-14"
    # arrows page by the range's own length (14 days)
    assert ctx["prev_from"] == "2026-06-17"
    assert ctx["prev_to"] == "2026-06-30"
    assert ctx["next_from"] == "2026-07-15"
    assert ctx["next_to"] == "2026-07-28"


def test_custom_single_day():
    ctx = custom_ctx("2026-07-01", "2026-07-01", TZ)
    assert ctx["t1"] - ctx["t0"] == 24 * 3600
    assert ctx["range_label"] == "Jul 1, 2026"


def test_custom_swapped_dates_normalized():
    ctx = custom_ctx("2026-07-14", "2026-07-01", TZ)
    assert ctx["range_label"] == "Jul 1 – Jul 14, 2026"


def test_custom_datetime_span():
    """Drag-to-zoom sends minute-granular datetimes; span stays exact."""
    ctx = custom_ctx("2026-07-10T14:30", "2026-07-10T16:45", TZ)
    assert ctx["t0"] == _local(datetime.date(2026, 7, 10), 14, 30)
    assert ctx["t1"] == _local(datetime.date(2026, 7, 10), 16, 45)
    assert ctx["range_label"] == "Jul 10, 2026 · 14:30 – 16:45"
    # arrows keep minute granularity and page by 2h15m
    assert ctx["prev_from"] == "2026-07-10T12:15"
    assert ctx["prev_to"] == "2026-07-10T14:30"
    # picker inputs degrade to whole days
    assert ctx["from_date"] == "2026-07-10"
    assert ctx["to_date"] == "2026-07-10"


def test_custom_datetime_across_days():
    ctx = custom_ctx("2026-07-10T22:00", "2026-07-11T02:00", TZ)
    assert ctx["range_label"] == "Jul 10 22:00 – Jul 11 02:00, 2026"


def test_custom_caps_t1_at_now():
    today = datetime.datetime.now(ZoneInfo(TZ)).date()
    ctx = custom_ctx((today - datetime.timedelta(days=1)).isoformat(),
                     today.isoformat(), TZ)
    assert ctx["t1"] <= time.time() + 1
    assert ctx["next_from"] is None       # nothing after "now"


def test_custom_malformed_raises():
    with pytest.raises(ValueError):
        custom_ctx("bogus", None, TZ)
    with pytest.raises(ValueError):
        custom_ctx(None, None, TZ)


def test_custom_spans_dst_change():
    # CET → CEST on 2026-03-29: the range is 1 h shorter than naive 3 days
    ctx = custom_ctx("2026-03-28", "2026-03-30", TZ)
    assert ctx["t1"] - ctx["t0"] == 3 * 24 * 3600 - 3600


def test_resolve_range_day_unchanged():
    t0, t1, _ = resolve_range("day", "2026-07-01", TZ)
    assert t1 - t0 == 24 * 3600


def test_resolve_range_relative():
    t0, t1, label = resolve_range("24h", None, TZ)
    assert abs((t1 - t0) - 24 * 3600) < 1
    assert abs(t1 - time.time()) < 5
    assert label == "last 24 h"
