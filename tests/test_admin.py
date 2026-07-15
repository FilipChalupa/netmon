"""Temporary admin action: delete-day endpoint (token gate, day-bounded wipe)."""

import time

import pytest


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NETMON_DB", str(tmp_path / "netmon.db"))
    monkeypatch.setenv("NETMON_MONITORS", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("NETMON_ADMIN_TOKEN", "admin-tajemstvi")
    monkeypatch.setenv("NETMON_ALERTS", "0")
    from fastapi.testclient import TestClient
    from netmon_server.main import app
    with TestClient(app) as c:
        yield c


def _seed(db_path):
    from netmon_server.db import connect, get_or_create_network
    conn = connect(db_path)
    nid = get_or_create_network(conn, "testnet", "Test")
    now = time.time()
    for offset, iso in ((0, "today"), (-86400 * 2, "older")):
        conn.execute("INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status, rtt_ms) "
                     "VALUES(?,?,?, 'google', 'ok', 10.0)", (nid, now + offset, iso))
        conn.execute("INSERT INTO uptime(network_id, ts_epoch, ts_iso, event) "
                     "VALUES(?,?,?, 'ALIVE')", (nid, now + offset, iso))
    conn.commit()
    return conn, nid


def test_delete_day_wipes_only_that_day(client, tmp_path):
    import datetime
    conn, nid = _seed(str(tmp_path / "netmon.db"))
    today = datetime.date.today().isoformat()

    resp = client.post(f"/net/testnet/delete-day",
                       data={"token": "admin-tajemstvi", "date": today},
                       follow_redirects=False)
    assert resp.status_code == 303

    left = conn.execute("SELECT ts_iso FROM latency WHERE network_id=?", (nid,)).fetchall()
    assert [r["ts_iso"] for r in left] == ["older"]
    assert conn.execute("SELECT COUNT(*) c FROM uptime WHERE network_id=?",
                        (nid,)).fetchone()["c"] == 1


def test_delete_day_requires_valid_token(client, tmp_path):
    import datetime
    conn, nid = _seed(str(tmp_path / "netmon.db"))
    resp = client.post(f"/net/testnet/delete-day",
                       data={"token": "spatny", "date": datetime.date.today().isoformat()},
                       follow_redirects=False)
    assert resp.status_code == 403
    assert conn.execute("SELECT COUNT(*) c FROM latency").fetchone()["c"] == 2
