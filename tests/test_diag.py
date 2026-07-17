"""Outage traceroute capture: detection, command building, event attachment."""

import threading

import pytest

from netmon_monitor import probes as mon_probes
from netmon_monitor import workers
from netmon_monitor.config import Config
from netmon_monitor.db import Db, fetch_after
from netmon_monitor.probes import _traceroute_cmd
from netmon_monitor.workers import internet_outage_active
from netmon_server.aggregate import attach_diags
from netmon_server.db import connect, get_or_create_network, init_db

GW = {"gateway"}
PUB = ["quad9", "google"]


def _rounds(*specs):
    """specs: per-round dict target→status; expands to (ts, target, status)."""
    rows = []
    for i, spec in enumerate(specs):
        for target, status in spec.items():
            rows.append((1000.0 + i * 2, target, status))
    return rows


def test_outage_detection_positive():
    bad = {"gateway": "ok", "quad9": "LOSS", "google": "LOSS"}
    assert internet_outage_active(_rounds(bad, bad, bad), GW, PUB, 3) is True


def test_gateway_loss_is_local_not_internet():
    bad = {"gateway": "LOSS", "quad9": "LOSS", "google": "LOSS"}
    assert internet_outage_active(_rounds(bad, bad, bad), GW, PUB, 3) is False


def test_single_target_loss_is_noise():
    one = {"gateway": "ok", "quad9": "LOSS", "google": "ok"}
    assert internet_outage_active(_rounds(one, one, one), GW, PUB, 3) is False


def test_needs_min_rounds():
    bad = {"gateway": "ok", "quad9": "LOSS", "google": "LOSS"}
    assert internet_outage_active(_rounds(bad, bad), GW, PUB, 3) is False


def test_recovery_breaks_the_run():
    bad = {"gateway": "ok", "quad9": "LOSS", "google": "LOSS"}
    ok = {"gateway": "ok", "quad9": "ok", "google": "ok"}
    assert internet_outage_active(_rounds(bad, bad, ok), GW, PUB, 3) is False


def test_traceroute_cmd_per_platform(monkeypatch):
    assert _traceroute_cmd("9.9.9.9", 15, "Windows")[0] == "tracert"
    assert _traceroute_cmd("9.9.9.9", 15, "Darwin")[0] == "traceroute"
    monkeypatch.setattr(mon_probes.shutil, "which",
                        lambda t: "/usr/bin/tracepath" if t == "tracepath" else None)
    assert _traceroute_cmd("9.9.9.9", 15, "Linux")[0] == "tracepath"
    monkeypatch.setattr(mon_probes.shutil, "which", lambda t: None)
    assert _traceroute_cmd("9.9.9.9", 15, "Linux") is None


def test_diag_loop_fires_once_per_cooldown(tmp_path, monkeypatch):
    db = Db(str(tmp_path / "monitor.db"))
    import time as _time
    now = _time.time()
    for i in range(4):  # ongoing internet outage in the local DB
        for tgt, st in (("gateway", "ok"), ("quad9", "LOSS"), ("google", "LOSS")):
            db.insert_latency(now - 6 + i * 2, "T", tgt, "1.1.1.1", st, None)
    calls = []
    monkeypatch.setattr(mon_probes, "traceroute",
                        lambda ip, **kw: calls.append(ip) or f"route to {ip}")

    stop = threading.Event()
    rounds = {"n": 0}

    def wait(timeout=None):
        rounds["n"] += 1
        if rounds["n"] >= 3:   # 3 iterations: fire, then cooldown suppresses
            stop.set()
        return stop.is_set()

    monkeypatch.setattr(stop, "wait", wait)
    workers.diag_loop(Config(), db, stop)
    db.close()

    rows, _ = fetch_after(str(tmp_path / "monitor.db"), "diag", 0, 100)
    assert [r["target"] for r in rows] == ["quad9", "google"]   # once, not thrice
    assert "route to" in rows[0]["output"]


def test_attach_diags_window(tmp_path):
    db_path = str(tmp_path / "server.db")
    init_db(db_path)
    conn = connect(db_path)
    nid = get_or_create_network(conn, "n", "N")
    for i, ts in enumerate((1010.0, 1500.0)):   # in-outage, and far outside
        conn.execute("INSERT INTO diag(network_id, src_id, ts_epoch, ts_iso, "
                     "target, output) VALUES(?,?,?,?,?,?)",
                     (nid, i + 1, ts, f"T{i}", "quad9", f"trace {i}"))
    conn.commit()
    events = [{"start_epoch": 1000.0, "end_epoch": 1060.0}]
    out = attach_diags(conn, nid, events, 900.0, 2000.0)
    assert [d["output"] for d in out[0]["diags"]] == ["trace 0"]
    conn.close()
