"""Alerting: outage threshold + dedup, monitor offline + recovery."""

import time

import pytest

from netmon_server import alerts
from netmon_server.config import MonitorCfg, ServerConfig
from netmon_server.db import connect, get_or_create_network, init_db


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def cfg():
    return ServerConfig(monitors=[MonitorCfg(name="testnet", url="http://x")],
                        alert_min_outage_s=60, alert_offline_s=600)


@pytest.fixture
def sent(monkeypatch):
    """Captures send_email calls inside alerts.py; pretends sending succeeded."""
    calls = []
    monkeypatch.setattr(alerts, "send_email",
                        lambda subject, body, **kw: calls.append((subject, body)) or True)
    return calls


def _internet_outage(conn, nid, start, rounds):
    """Insert `rounds` consecutive ping rounds (2 s apart) with both public targets lost."""
    for i in range(rounds):
        ts = start + 2 * i
        for target in ("quad9", "google"):
            conn.execute(
                "INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status) "
                "VALUES(?,?,?,?, 'LOSS')", (nid, ts, f"T{ts:.0f}", target))
    conn.commit()


def _reach_fails(conn, nid, start, count, status="FAIL"):
    """Insert `count` consecutive reach probes (30 s apart)."""
    for i in range(count):
        ts = start + 30 * i
        conn.execute(
            "INSERT INTO reach(network_id, ts_epoch, ts_iso, status) VALUES(?,?,?,?)",
            (nid, ts, f"R{ts:.0f}", status))
    conn.commit()


def test_outage_alert_and_dedup(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _internet_outage(conn, nid, now - 300, rounds=60)  # 118 s outage → above threshold

    assert len(alerts.check_once(conn, cfg, now)) == 1
    assert "outage on Test net" in sent[0][0] and "internet" in sent[0][0]

    # second pass: the same event must not alert again
    assert alerts.check_once(conn, cfg, now) == []
    assert len(sent) == 1


def test_short_outage_is_ignored(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _internet_outage(conn, nid, now - 300, rounds=5)  # 8 s outage → below 60 s threshold

    assert alerts.check_once(conn, cfg, now) == []
    assert sent == []


def test_ongoing_outage_alerts_once(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _internet_outage(conn, nid, now - 200, rounds=60)
    assert len(alerts.check_once(conn, cfg, now)) == 1

    # the outage continues (same start → same dedup key)
    _internet_outage(conn, nid, now - 200 + 120, rounds=30)
    assert alerts.check_once(conn, cfg, now + 60) == []
    assert len(sent) == 1


def test_reach_alert_and_dedup(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _reach_fails(conn, nid, now - 600, count=12)  # 12 consecutive FAILs ≥ threshold 10

    assert len(alerts.check_once(conn, cfg, now)) == 1
    assert "reach probes down" in sent[0][0]
    assert alerts.check_once(conn, cfg, now) == []  # dedup
    assert len(sent) == 1


def test_short_reach_run_is_ignored(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _reach_fails(conn, nid, now - 600, count=5)     # below threshold
    _reach_fails(conn, nid, now - 400, count=3, status="ok")
    assert alerts.check_once(conn, cfg, now) == []


def test_reach_alert_suppressed_during_ping_outage(conn, cfg, sent):
    """A hard outage alerts via the outage path; reach must not double-report."""
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _internet_outage(conn, nid, now - 600, rounds=200)   # ~400 s outage
    _reach_fails(conn, nid, now - 590, count=12)         # overlaps the outage

    subjects = alerts.check_once(conn, cfg, now)
    assert len(subjects) == 1
    assert "outage on" in subjects[0]                    # only the outage email


def test_offline_alert_and_recovery(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    conn.execute("INSERT INTO sync_status(network_id, last_ok_at) VALUES(?,?)",
                 (nid, now - 3600))
    conn.commit()

    assert len(alerts.check_once(conn, cfg, now)) == 1
    assert "unreachable" in sent[0][0]
    assert alerts.check_once(conn, cfg, now) == []  # no repeat while still offline

    # monitor comes back → recovery email, then silence
    conn.execute("UPDATE sync_status SET last_ok_at=? WHERE network_id=?", (now, nid))
    conn.commit()
    assert len(alerts.check_once(conn, cfg, now)) == 1
    assert "back online" in sent[1][0]
    assert alerts.check_once(conn, cfg, now) == []


def test_never_synced_monitor_is_not_offline(conn, cfg, sent):
    get_or_create_network(conn, "testnet", "Test net")
    assert alerts.check_once(conn, cfg, time.time()) == []
    assert sent == []


def test_imported_only_network_never_offline(conn, cfg, sent):
    nid = get_or_create_network(conn, "archive", "Archive")
    conn.execute("INSERT INTO sync_status(network_id, last_ok_at) VALUES(?,?)",
                 (nid, time.time() - 9999))
    conn.commit()
    assert alerts.check_once(conn, cfg, time.time()) == []


def _speed_tests(conn, nid, t0, count, mbps, step=3600):
    for i in range(count):
        conn.execute(
            "INSERT INTO speed(network_id, ts_epoch, ts_iso, down_mbps) VALUES(?,?,?,?)",
            (nid, t0 + i * step, "", mbps))
    conn.commit()


def test_speed_degradation_alert_recovery_and_rearm(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _speed_tests(conn, nid, now - 40 * 3600, 30, 500.0)   # baseline ~500 Mbit/s
    _speed_tests(conn, nid, now - 3 * 3600, 3, 100.0)     # recent median 100 → 20 %

    subjects = alerts.check_once(conn, cfg, now)
    assert len(subjects) == 1 and "speed degraded" in subjects[0]
    assert "20% of usual" in subjects[0]

    # dedup: still degraded, no second email
    assert alerts.check_once(conn, cfg, now) == []

    # recovery: fresh fast tests lift the recent median above the threshold
    _speed_tests(conn, nid, now - 1500, 5, 480.0, step=300)
    subjects = alerts.check_once(conn, cfg, now)
    assert len(subjects) == 1 and "back to normal" in subjects[0]

    # re-armed and healthy → quiet
    assert alerts.check_once(conn, cfg, now) == []


def test_speed_alert_needs_baseline(conn, cfg, sent):
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _speed_tests(conn, nid, now - 3 * 3600, 3, 10.0)      # slow, but no baseline
    assert alerts.check_once(conn, cfg, now) == []


def test_speed_alert_disabled_by_zero_pct(conn, sent):
    cfg = ServerConfig(monitors=[MonitorCfg(name="testnet", url="http://x")],
                       alert_speed_pct=0)
    nid = get_or_create_network(conn, "testnet", "Test net")
    now = time.time()
    _speed_tests(conn, nid, now - 40 * 3600, 30, 500.0)
    _speed_tests(conn, nid, now - 3 * 3600, 3, 50.0)
    assert alerts.check_once(conn, cfg, now) == []
