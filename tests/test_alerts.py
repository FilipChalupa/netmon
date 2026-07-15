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
