"""Schema migrations: DBs created by older versions gain the new speed
columns (up_mbps 2.4, idle_rtt_ms/loaded_rtt_ms 2.5) on open, and inserts
into the migrated tables work."""

import sqlite3

from netmon_monitor.db import Db as MonitorDb
from netmon_server.db import connect, get_or_create_network, init_db, insert_sql

# the speed tables exactly as version 2.3.x created them
OLD_MONITOR_SPEED = """
CREATE TABLE speed(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    down_mbps REAL,
    bytes INTEGER,
    seconds REAL,
    http_code INTEGER
);
"""

OLD_SERVER_SPEED = """
CREATE TABLE speed(
    id INTEGER PRIMARY KEY,
    network_id INTEGER NOT NULL,
    src_id INTEGER,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT,
    down_mbps REAL,
    bytes INTEGER,
    seconds REAL,
    http_code INTEGER,
    UNIQUE(network_id, src_id)
);
"""

NEW_COLUMNS = ("up_mbps", "idle_rtt_ms", "loaded_rtt_ms")


def _columns(db_path: str) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(speed)")}
    finally:
        conn.close()


def test_monitor_db_migrates_old_speed_table(tmp_path):
    path = str(tmp_path / "monitor.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_MONITOR_SPEED)
    conn.execute("INSERT INTO speed(ts_epoch, ts_iso, down_mbps, bytes, seconds, "
                 "http_code) VALUES(1, 'T1', 100.0, 1, 1.0, 200)")
    conn.commit()
    conn.close()

    db = MonitorDb(path)
    try:
        assert set(NEW_COLUMNS) <= _columns(path)
        # old row survives with NULLs, new insert fills everything
        db.insert_speed(2, "T2", 200.0, 2, 2.0, 200, 50.0, 8.0, 31.0)
    finally:
        db.close()

    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT down_mbps, up_mbps, idle_rtt_ms, loaded_rtt_ms "
                        "FROM speed ORDER BY id").fetchall()
    conn.close()
    assert rows == [(100.0, None, None, None), (200.0, 50.0, 8.0, 31.0)]


def test_monitor_db_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "monitor.db")
    MonitorDb(path).close()
    MonitorDb(path).close()   # second open must not fail on existing columns
    assert set(NEW_COLUMNS) <= _columns(path)


def test_server_db_migrates_old_speed_table(tmp_path):
    path = str(tmp_path / "server.db")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SERVER_SPEED)
    conn.execute("INSERT INTO speed(network_id, src_id, ts_epoch, down_mbps) "
                 "VALUES(1, 1, 1, 100.0)")
    conn.commit()
    conn.close()

    init_db(path)
    assert set(NEW_COLUMNS) <= _columns(path)

    conn = connect(path)
    try:
        nid = get_or_create_network(conn, "net")
        conn.execute(insert_sql("speed"),
                     (nid, 2, 2.0, "T2", 200.0, 2, 2.0, 200, 50.0, 8.0, 31.0))
        conn.commit()
        rows = conn.execute("SELECT down_mbps, up_mbps, loaded_rtt_ms "
                            "FROM speed ORDER BY src_id").fetchall()
        assert [tuple(r) for r in rows] == [(100.0, None, None), (200.0, 50.0, 31.0)]
    finally:
        conn.close()


def test_server_db_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "server.db")
    init_db(path)
    init_db(path)
    assert set(NEW_COLUMNS) <= _columns(path)
