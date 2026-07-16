"""Public IP tracking: change-only recording on the monitor, history on the server."""

import threading

import pytest

from netmon_monitor import probes as mon_probes
from netmon_monitor import workers
from netmon_monitor.config import Config
from netmon_monitor.db import Db, fetch_after
from netmon_server import aggregate
from netmon_server.db import connect, get_or_create_network, init_db


def test_pubip_loop_records_only_changes(tmp_path, monkeypatch):
    db = Db(str(tmp_path / "monitor.db"))
    ips = iter(["1.1.1.1", "1.1.1.1", "2.2.2.2", None, "2.2.2.2"])
    monkeypatch.setattr(mon_probes, "public_ip", lambda url, timeout=10.0: next(ips))

    stop = threading.Event()
    rounds = {"n": 0}

    def wait(timeout=None):
        rounds["n"] += 1
        if rounds["n"] >= 5:
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    workers.pubip_loop(Config(), db, stop)
    db.close()

    rows, _ = fetch_after(str(tmp_path / "monitor.db"), "pubip", 0, 100)
    assert [r["ip"] for r in rows] == ["1.1.1.1", "2.2.2.2"]


def test_pubip_loop_seeds_from_db(tmp_path, monkeypatch):
    """After a restart the last recorded IP must not be recorded again."""
    path = str(tmp_path / "monitor.db")
    db = Db(path)
    db.insert_pubip(1000.0, "T", "1.1.1.1")
    monkeypatch.setattr(mon_probes, "public_ip", lambda url, timeout=10.0: "1.1.1.1")

    stop = threading.Event()
    monkeypatch.setattr(stop, "wait", lambda timeout=None: stop.set() or True)
    workers.pubip_loop(Config(), db, stop)
    db.close()

    rows, _ = fetch_after(path, "pubip", 0, 100)
    assert len(rows) == 1


@pytest.fixture
def server_conn(tmp_path):
    db_path = str(tmp_path / "server.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def test_pubip_history(server_conn, monkeypatch):
    monkeypatch.setattr(aggregate, "ptr_lookup", lambda ip, timeout=1.5: "x.isp.example")
    nid = get_or_create_network(server_conn, "home", "Home")
    for i, (ts, ip) in enumerate([(100.0, "1.1.1.1"), (500.0, "2.2.2.2"),
                                  (900.0, "3.3.3.3")]):
        server_conn.execute(
            "INSERT INTO pubip(network_id, src_id, ts_epoch, ts_iso, ip) "
            "VALUES(?,?,?,?,?)", (nid, i + 1, ts, f"T{i}", ip))
    server_conn.commit()

    h = aggregate.pubip_history(server_conn, nid, 400.0, 1000.0)
    assert h["current"]["ip"] == "3.3.3.3"
    assert h["current"]["ptr"] == "x.isp.example"
    assert [c["ip"] for c in h["changes"]] == ["2.2.2.2", "3.3.3.3"]

    # the IP effective at t1 predates the range → current set, no changes
    h2 = aggregate.pubip_history(server_conn, nid, 200.0, 400.0)
    assert h2["current"]["ip"] == "1.1.1.1"
    assert h2["changes"] == []

    # nothing recorded yet
    nid2 = get_or_create_network(server_conn, "empty", "Empty")
    h3 = aggregate.pubip_history(server_conn, nid2, 0, 1000.0)
    assert h3["current"] is None
    assert h3["changes"] == []
