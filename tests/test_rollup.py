"""Hourly latency rollups must give the same answers as raw-row scans."""

import datetime

from netmon_server import rollup
from netmon_server.aggregate import daily_heatmap, latency_series, pick_bucket, summary
from netmon_server.db import connect, get_or_create_network, init_db

T0 = 1_750_000_000 - 1_750_000_000 % 3600  # hour-aligned start


def _seed(conn, net_id, hours=30, interval=60):
    """Pings for `hours` hours: gateway always ok, quad9/google with a loss
    pattern and a missing-rtt LOSS row mix."""
    rows = []
    for i in range(hours * 3600 // interval):
        ts = T0 + i * interval
        iso = datetime.datetime.utcfromtimestamp(ts).isoformat()
        for tgt in ("gateway", "quad9", "google"):
            lost = tgt != "gateway" and i % 50 == 0
            rows.append((net_id, ts, iso, tgt,
                         "LOSS" if lost else "ok",
                         None if lost else 10.0 + (i % 7)))
    conn.executemany(
        "INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status, rtt_ms) "
        "VALUES(?,?,?,?,?,?)", rows)
    conn.commit()


def _mk(tmp_path):
    path = str(tmp_path / "server.db")
    init_db(path)
    conn = connect(path)
    net_id = get_or_create_network(conn, "home", "Home")
    _seed(conn, net_id)
    return conn, net_id


def test_series_rollup_matches_raw(tmp_path):
    conn, net_id = _mk(tmp_path)
    t0, t1 = T0, T0 + 30 * 3600
    bucket = 3600
    raw = latency_series(conn, net_id, t0, t1, bucket)   # not ready → raw path
    rollup.rebuild_network(conn, net_id)
    assert rollup.is_ready(conn, net_id)
    rolled = latency_series(conn, net_id, t0, t1, bucket)
    assert rolled == raw


def test_summary_rollup_matches_raw(tmp_path):
    conn, net_id = _mk(tmp_path)
    t0, t1 = T0, T0 + 30 * 3600
    raw = summary(conn, net_id, t0, t1)["targets"]
    rollup.rebuild_network(conn, net_id)
    rolled = summary(conn, net_id, t0, t1)["targets"]
    assert rolled == raw


def test_heatmap_rollup_matches_raw(tmp_path):
    conn, net_id = _mk(tmp_path)
    end = datetime.date.fromtimestamp(T0 + 30 * 3600) + datetime.timedelta(days=2)
    raw = daily_heatmap(conn, net_id, "Europe/Prague", days=7, end_day=end)
    rollup.rebuild_network(conn, net_id)
    rolled = daily_heatmap(conn, net_id, "Europe/Prague", days=7, end_day=end)
    assert rolled == raw


def test_incremental_update_matches_rebuild(tmp_path):
    """update_hours over a sub-window must leave the same table content as a
    full rebuild — the sync hook relies on this."""
    conn, net_id = _mk(tmp_path)
    rollup.rebuild_network(conn, net_id)
    full = conn.execute("SELECT * FROM latency_hourly ORDER BY target, hour").fetchall()
    # wipe two hours in the middle, then repair just that window
    conn.execute("DELETE FROM latency_hourly WHERE hour>=? AND hour<?",
                 (T0 + 5 * 3600, T0 + 7 * 3600))
    conn.commit()
    rollup.update_hours(conn, net_id, T0 + 5 * 3600 + 120, T0 + 6 * 3600 + 120)
    repaired = conn.execute("SELECT * FROM latency_hourly ORDER BY target, hour").fetchall()
    assert [tuple(r) for r in repaired] == [tuple(r) for r in full]


def test_pick_bucket_snaps_long_ranges_to_hours():
    assert pick_bucket(0, 86400) == 60          # a day stays per-minute
    for span in (30 * 86400, 90 * 86400, 365 * 86400):
        assert pick_bucket(0, span) % 3600 == 0
    assert pick_bucket(0, 30 * 86400) == 3600   # a month = hourly points
