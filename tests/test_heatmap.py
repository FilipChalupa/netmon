"""Calendar heatmap: per-local-day loss aggregation on the public targets."""

import datetime
from zoneinfo import ZoneInfo

import pytest

from netmon_server.aggregate import daily_heatmap
from netmon_server.db import connect, get_or_create_network, init_db

TZ = "Europe/Prague"


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def _insert(conn, net_id, epoch, target, status):
    conn.execute("INSERT INTO latency(network_id, ts_epoch, ts_iso, target, "
                 "status, rtt_ms) VALUES(?,?,?,?,?,?)",
                 (net_id, epoch, "", target, status,
                  None if status == "LOSS" else 10.0))


def _epoch(day, hour):
    return datetime.datetime.combine(day, datetime.time(hour),
                                     ZoneInfo(TZ)).timestamp()


def test_daily_loss_and_gaps(conn):
    nid = get_or_create_network(conn, "n", "N")
    end = datetime.date(2026, 7, 10)
    d1 = end - datetime.timedelta(days=2)
    # d1: 10 public samples, 2 lost = 20 %; gateway rows must not count
    for i in range(4):
        _insert(conn, nid, _epoch(d1, 10) + i, "quad9", "OK")
        _insert(conn, nid, _epoch(d1, 10) + i, "google", "OK")
    _insert(conn, nid, _epoch(d1, 11), "quad9", "LOSS")
    _insert(conn, nid, _epoch(d1, 11), "google", "LOSS")
    _insert(conn, nid, _epoch(d1, 11), "gateway", "LOSS")
    conn.commit()

    days = daily_heatmap(conn, nid, TZ, days=3, end_day=end)
    assert [d["day"] for d in days] == [d1.isoformat(),
                                        (end - datetime.timedelta(days=1)).isoformat(),
                                        end.isoformat()]
    assert days[0]["samples"] == 10
    assert days[0]["loss"] == 20.0
    assert days[1] == {"day": days[1]["day"], "samples": 0, "loss": None}
    assert days[2]["loss"] is None


def test_local_midnight_boundary(conn):
    """23:30 and 00:30 local time land on different heatmap days."""
    nid = get_or_create_network(conn, "n", "N")
    end = datetime.date(2026, 7, 10)
    d1 = end - datetime.timedelta(days=1)
    before = datetime.datetime.combine(d1, datetime.time(23, 30),
                                       ZoneInfo(TZ)).timestamp()
    after = before + 3600  # 00:30 local on `end`
    _insert(conn, nid, before, "quad9", "OK")
    _insert(conn, nid, after, "quad9", "LOSS")
    conn.commit()

    days = daily_heatmap(conn, nid, TZ, days=2, end_day=end)
    assert days[0]["day"] == d1.isoformat()
    assert (days[0]["samples"], days[0]["loss"]) == (1, 0.0)
    assert (days[1]["samples"], days[1]["loss"]) == (1, 100.0)


def test_range_excludes_older_data(conn):
    nid = get_or_create_network(conn, "n", "N")
    end = datetime.date(2026, 7, 10)
    old = end - datetime.timedelta(days=5)
    _insert(conn, nid, _epoch(old, 12), "quad9", "OK")
    conn.commit()
    days = daily_heatmap(conn, nid, TZ, days=3, end_day=end)
    assert all(d["samples"] == 0 for d in days)
