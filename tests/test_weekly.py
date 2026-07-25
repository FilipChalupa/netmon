"""Weekly digest: two-week comparison text and the due-date logic."""

import datetime
import time

from netmon_server.config import ServerConfig
from netmon_server.db import connect, get_or_create_network, init_db
from netmon_server.report import build_weekly, weekly_due


def _seed_week(conn, nid, t0, days, rtt, down, up, loaded):
    """One latency round + one speed test per day — enough for summary()."""
    for d in range(days):
        ts = t0 + d * 86400 + 12 * 3600
        for tgt in ("gateway", "quad9", "google"):
            conn.execute(
                "INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status, "
                "rtt_ms) VALUES(?,?,?,?, 'ok', ?)", (nid, ts, "", tgt, rtt))
        conn.execute(
            "INSERT INTO speed(network_id, ts_epoch, ts_iso, down_mbps, up_mbps, "
            "idle_rtt_ms, loaded_rtt_ms) VALUES(?,?,?,?,?,?,?)",
            (nid, ts, "", down, up, 8.0, loaded))
    conn.commit()


def test_build_weekly_compares_two_weeks(tmp_path):
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    conn = connect(db_path)
    nid = get_or_create_network(conn, "home", "Home")
    conn.execute("UPDATE networks SET description='FTTH 500/50' WHERE id=?", (nid,))

    cfg = ServerConfig(db_path=db_path)
    monday = datetime.date(2026, 7, 13)
    week_t0 = datetime.datetime(2026, 7, 13, tzinfo=datetime.timezone.utc).timestamp()
    _seed_week(conn, nid, week_t0 - 7 * 86400, 7, rtt=8.0, down=520.0, up=52.0, loaded=20.0)
    _seed_week(conn, nid, week_t0, 7, rtt=9.0, down=260.0, up=50.0, loaded=120.0)
    conn.close()

    subject, text = build_weekly(cfg, monday)
    assert subject == "netmon weekly digest 2026-07-13 – 2026-07-19"
    assert "== Home ==" in text and "(FTTH 500/50)" in text
    assert "download avg:" in text and "-50 %" in text     # 260 vs 520
    assert "prev 520 Mbit/s" in text
    assert "bufferbloat avg:" in text and "+112 ms" in text
    assert "coverage:" in text


def test_build_weekly_no_data_returns_none(tmp_path):
    db_path = str(tmp_path / "t.db")
    init_db(db_path)
    connect(db_path).close()
    assert build_weekly(ServerConfig(db_path=db_path),
                        datetime.date(2026, 7, 13)) is None


def test_weekly_due():
    wed = datetime.date(2026, 7, 22)                       # Wednesday
    prev_monday = datetime.date(2026, 7, 13)               # last complete week
    assert weekly_due(None, wed) == prev_monday            # never sent → due
    assert weekly_due("2026-07-06", wed) == prev_monday    # older week sent → due
    assert weekly_due("2026-07-13", wed) is None           # already sent → quiet
    # Monday morning: the week that just ended is due
    assert weekly_due("2026-07-13", datetime.date(2026, 7, 27)) \
        == datetime.date(2026, 7, 20)
