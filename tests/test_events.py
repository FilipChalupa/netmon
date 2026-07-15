"""Outage derivation: expected intervals + parity with legacy/events.sh."""

import csv
import shutil
import subprocess
from pathlib import Path

import pytest

from netmon_server.db import connect, get_or_create_network, init_db
from netmon_server.events import derive_events
from netmon_server.importer import import_tree

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "log"
LEGACY_EVENTS = Path(__file__).parent.parent / "legacy" / "events.sh"


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def net_id(conn):
    nid = get_or_create_network(conn, "test", "Test")
    import_tree(conn, nid, str(FIXTURE_LOG), force=False)
    return nid


def test_expected_events(conn, net_id):
    events = derive_events(conn, net_id, 0, 2e12, ping_interval=2)
    got = [(e.start_iso[11:19], e.end_iso[11:19], e.duration_s, e.scope) for e in events]
    assert got == [
        ("10:00:02", "10:00:04", 2, "internet"),   # both public targets
        ("10:00:10", "10:00:12", 2, "local"),      # gateway (dominates even over internet loss)
        ("10:00:14", "10:00:14", 2, "internet"),   # scope change splits the interval
        ("10:00:20", "10:00:20", 2, "internet"),   # single-round outage → minimum duration
    ]


def test_single_public_loss_is_not_event(conn, net_id):
    # the 10:00:08 round (quad9 LOSS only) must not create an event
    events = derive_events(conn, net_id, 0, 2e12, ping_interval=2)
    assert all(e.start_iso[11:19] != "10:00:08" for e in events)


def test_gap_splits_event(conn):
    """Outages interrupted by a period when the monitor was down must not merge."""
    nid = get_or_create_network(conn, "gap", "Gap")
    rows = [
        (1000.0, "T1", "quad9"), (1000.0, "T1", "google"),
        (1002.0, "T2", "quad9"), (1002.0, "T2", "google"),
        # the monitor was down for 10 minutes
        (1602.0, "T3", "quad9"), (1602.0, "T3", "google"),
    ]
    for epoch, iso, target in rows:
        conn.execute(
            "INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status) "
            "VALUES(?,?,?,?, 'LOSS')", (nid, epoch, iso, target))
    conn.commit()
    events = derive_events(conn, nid, 0, 2e12, ping_interval=2)
    assert [(e.start_epoch, e.end_epoch) for e in events] == [(1000.0, 1002.0), (1602.0, 1602.0)]


@pytest.mark.skipif(not shutil.which("bash") or not LEGACY_EVENTS.exists(),
                    reason="bash or legacy/events.sh is not available")
def test_parity_with_legacy_events_sh(conn, net_id, tmp_path):
    """The Python port must produce the same intervals as the original events.sh."""
    workdir = tmp_path / "legacy"
    workdir.mkdir()
    shutil.copy(LEGACY_EVENTS, workdir / "events.sh")
    shutil.copytree(FIXTURE_LOG, workdir / "log")
    out = workdir / "events.csv"
    subprocess.run(
        ["bash", str(workdir / "events.sh")],
        env={"PATH": "/usr/bin:/bin", "NETMON_EVENTS_OUT": str(out)},
        cwd=workdir, capture_output=True, check=True,
    )
    with open(out, newline="") as f:
        legacy = [(r["start"][11:19], r["end"][11:19], int(r["duration_s"]), r["scope"])
                  for r in csv.DictReader(f)]
    events = derive_events(conn, net_id, 0, 2e12, ping_interval=2)
    ours = [(e.start_iso[11:19], e.end_iso[11:19], e.duration_s, e.scope) for e in events]
    assert ours == legacy
