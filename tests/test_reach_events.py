"""Reach-fail runs surfaced as events, merged with ping-derived outages."""

import pytest

from netmon_server.db import connect, get_or_create_network, init_db
from netmon_server.events import (Event, derive_reach_events, events_summary,
                                  merge_events)


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def net_id(conn):
    return get_or_create_network(conn, "n", "N")


def _reach(conn, nid, start, statuses, step=30):
    for i, st in enumerate(statuses):
        ts = start + i * step
        conn.execute("INSERT INTO reach(network_id, ts_epoch, ts_iso, status) "
                     "VALUES(?,?,?,?)", (nid, ts, f"R{ts:.0f}", st))
    conn.commit()


def test_run_of_fails_becomes_event(conn, net_id):
    _reach(conn, net_id, 1000, ["ok"] + ["FAIL"] * 10 + ["ok"])
    events = derive_reach_events(conn, net_id, 0, 2e12, min_fails=10)
    assert len(events) == 1
    e = events[0]
    assert e.scope == "reach"
    assert (e.start_epoch, e.end_epoch) == (1030.0, 1300.0)
    assert "pings OK" in e.note


def test_short_run_is_ignored(conn, net_id):
    _reach(conn, net_id, 1000, ["FAIL"] * 9 + ["ok"])
    assert derive_reach_events(conn, net_id, 0, 2e12, min_fails=10) == []


def test_monitor_gap_splits_run(conn, net_id):
    """Two 6-fail runs separated by a dead-monitor hole must not merge."""
    _reach(conn, net_id, 1000, ["FAIL"] * 6)
    _reach(conn, net_id, 2000, ["FAIL"] * 6)   # 850 s gap > 150 s threshold
    assert derive_reach_events(conn, net_id, 0, 2e12, min_fails=10) == []
    # each part alone is below the threshold; with a low threshold both appear
    events = derive_reach_events(conn, net_id, 0, 2e12, min_fails=5)
    assert [(e.start_epoch, e.end_epoch) for e in events] == \
        [(1000.0, 1150.0), (2000.0, 2150.0)]


def test_merge_suppresses_overlap_with_ping_outage(conn, net_id):
    _reach(conn, net_id, 1000, ["FAIL"] * 10)
    reach = derive_reach_events(conn, net_id, 0, 2e12, min_fails=10)
    ping = [Event(1100.0, 1200.0, "P0", "P1", 100, "internet")]
    merged = merge_events(ping, reach)
    assert [e.scope for e in merged] == ["internet"]   # reach run absorbed

    # a disjoint ping outage keeps both, ordered by start
    ping2 = [Event(5000.0, 5100.0, "P0", "P1", 100, "local")]
    merged2 = merge_events(ping2, reach)
    assert [(e.scope, e.start_epoch) for e in merged2] == \
        [("reach", 1000.0), ("local", 5000.0)]


def test_events_summary_includes_reach(conn, net_id):
    _reach(conn, net_id, 1000, ["FAIL"] * 10)
    events = derive_reach_events(conn, net_id, 0, 2e12, min_fails=10)
    s = events_summary(events)
    assert s["reach"]["count"] == 1
