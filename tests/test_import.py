"""Importer: idempotence, --force, sentinel řádky, mapování hodnot."""

from pathlib import Path

import pytest

from netmon_server.db import connect, get_or_create_network, init_db
from netmon_server.importer import import_tree

FIXTURE_LOG = str(Path(__file__).parent / "fixtures" / "log")


@pytest.fixture
def conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def counts(conn, nid):
    return {t: conn.execute(f"SELECT COUNT(*) c FROM {t} WHERE network_id=?", (nid,)).fetchone()["c"]
            for t in ("latency", "reach", "speed", "uptime")}


def test_import_and_idempotence(conn):
    nid = get_or_create_network(conn, "doma", "Doma")
    s1 = import_tree(conn, nid, FIXTURE_LOG, force=False)
    c1 = counts(conn, nid)
    # 36 datových řádků latence (sentinel '--' přeskočen), 3 reach, 3 speed, 5 uptime
    assert c1 == {"latency": 36, "reach": 3, "speed": 3, "uptime": 5}
    assert s1["rows"] == 47

    # druhý import: soubory se přeskočí, počty se nezmění
    s2 = import_tree(conn, nid, FIXTURE_LOG, force=False)
    assert s2["rows"] == 0 and s2["skipped"] == 4
    assert counts(conn, nid) == c1

    # --force: znovu naimportuje, ale bez duplikátů
    s3 = import_tree(conn, nid, FIXTURE_LOG, force=True)
    assert s3["rows"] == 47
    assert counts(conn, nid) == c1


def test_value_mapping(conn):
    nid = get_or_create_network(conn, "doma", "Doma")
    import_tree(conn, nid, FIXTURE_LOG, force=False)

    # LOSS řádek má NULL rtt
    r = conn.execute("SELECT rtt_ms FROM latency WHERE network_id=? AND status='LOSS' LIMIT 1",
                     (nid,)).fetchone()
    assert r["rtt_ms"] is None

    # speed FAIL: down_mbps NULL, http_code 'FAIL' → 0
    fail = conn.execute("SELECT down_mbps, http_code FROM speed "
                        "WHERE network_id=? AND down_mbps IS NULL", (nid,)).fetchone()
    assert fail["http_code"] == 0

    # reach FAIL: '000' → 0, časy NULL
    rf = conn.execute("SELECT dns_ms, http_code FROM reach "
                      "WHERE network_id=? AND status='FAIL'", (nid,)).fetchone()
    assert rf["dns_ms"] is None and rf["http_code"] == 0

    # import nesahá na řádky ze syncu (src_id NOT NULL)
    conn.execute("INSERT INTO latency(network_id, src_id, ts_epoch, ts_iso, target, status) "
                 "VALUES(?, 999, 1.0, '2025-07-01T00:00:00+02:00', 'gateway', 'ok')", (nid,))
    conn.commit()
    import_tree(conn, nid, FIXTURE_LOG, force=True)
    assert conn.execute("SELECT COUNT(*) c FROM latency WHERE src_id=999").fetchone()["c"] == 1


def test_two_networks_are_separate(conn):
    a = get_or_create_network(conn, "doma", "Doma")
    b = get_or_create_network(conn, "chata", "Chata")
    import_tree(conn, a, FIXTURE_LOG, force=False)
    import_tree(conn, b, FIXTURE_LOG, force=False)
    assert counts(conn, a)["latency"] == 36
    assert counts(conn, b)["latency"] == 36
