"""SQLite schema and helpers of the evaluation server.

Idempotent sync: rows pulled from monitors carry src_id (the monitor's row
id) and UNIQUE(network_id, src_id) + INSERT OR IGNORE guarantees nothing is
ever duplicated. Imported CSV rows have src_id NULL (SQLite UNIQUE ignores
NULLs — import dedup happens at file level in the imports table).
"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS networks(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    label TEXT
);

CREATE TABLE IF NOT EXISTS latency(
    id INTEGER PRIMARY KEY,
    network_id INTEGER NOT NULL REFERENCES networks(id),
    src_id INTEGER,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT,
    target TEXT NOT NULL,
    ip TEXT,
    status TEXT NOT NULL,
    rtt_ms REAL,
    UNIQUE(network_id, src_id)
);
CREATE INDEX IF NOT EXISTS idx_latency_net_ts ON latency(network_id, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_latency_loss ON latency(network_id, ts_epoch) WHERE status='LOSS';

CREATE TABLE IF NOT EXISTS reach(
    id INTEGER PRIMARY KEY,
    network_id INTEGER NOT NULL REFERENCES networks(id),
    src_id INTEGER,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT,
    dns_ms REAL,
    tcp_ms REAL,
    tls_ms REAL,
    http_code INTEGER,
    status TEXT NOT NULL,
    UNIQUE(network_id, src_id)
);
CREATE INDEX IF NOT EXISTS idx_reach_net_ts ON reach(network_id, ts_epoch);

CREATE TABLE IF NOT EXISTS speed(
    id INTEGER PRIMARY KEY,
    network_id INTEGER NOT NULL REFERENCES networks(id),
    src_id INTEGER,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT,
    down_mbps REAL,
    bytes INTEGER,
    seconds REAL,
    http_code INTEGER,
    UNIQUE(network_id, src_id)
);
CREATE INDEX IF NOT EXISTS idx_speed_net_ts ON speed(network_id, ts_epoch);

CREATE TABLE IF NOT EXISTS uptime(
    id INTEGER PRIMARY KEY,
    network_id INTEGER NOT NULL REFERENCES networks(id),
    src_id INTEGER,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT,
    event TEXT NOT NULL,
    UNIQUE(network_id, src_id)
);
CREATE INDEX IF NOT EXISTS idx_uptime_net_ts ON uptime(network_id, ts_epoch);

CREATE TABLE IF NOT EXISTS sync_cursor(
    network_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    last_src_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL,
    PRIMARY KEY(network_id, kind)
);

CREATE TABLE IF NOT EXISTS sync_status(
    network_id INTEGER PRIMARY KEY,
    last_ok_at REAL,
    last_attempt_at REAL,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS imports(
    network_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    sha256 TEXT,
    path TEXT,
    imported_at REAL,
    PRIMARY KEY(network_id, kind, day)
);

CREATE TABLE IF NOT EXISTS meta(
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS alerts(
    network_id INTEGER NOT NULL,
    kind TEXT NOT NULL,           -- 'outage' | 'offline'
    key TEXT NOT NULL,            -- outage: event start_epoch; offline: 'state'
    sent_at REAL,
    PRIMARY KEY(network_id, kind, key)
);

CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    text TEXT NOT NULL,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts_epoch);

-- which networks a note applies to; no rows = general note (applies to all)
CREATE TABLE IF NOT EXISTS note_networks(
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    network_id INTEGER NOT NULL REFERENCES networks(id),
    PRIMARY KEY(note_id, network_id)
);
"""

KINDS = ("latency", "reach", "speed", "uptime")

# columns inserted during sync/import (without id), in INSERT order
KIND_INSERT_COLUMNS = {
    "latency": ["network_id", "src_id", "ts_epoch", "ts_iso", "target", "ip", "status", "rtt_ms"],
    "reach": ["network_id", "src_id", "ts_epoch", "ts_iso", "dns_ms", "tcp_ms", "tls_ms", "http_code", "status"],
    "speed": ["network_id", "src_id", "ts_epoch", "ts_iso", "down_mbps", "bytes", "seconds", "http_code"],
    "uptime": ["network_id", "src_id", "ts_epoch", "ts_iso", "event"],
}


def insert_sql(kind: str) -> str:
    cols = KIND_INSERT_COLUMNS[kind]
    return (f"INSERT OR IGNORE INTO {kind}({', '.join(cols)}) "
            f"VALUES({', '.join('?' * len(cols))})")


def connect(path: str) -> sqlite3.Connection:
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        # migration: imports used to be keyed by (network_id, path) — content-hash
        # dedup needs (network_id, kind, day). Dropping is safe: importing a file
        # again first deletes that day's imported rows, so nothing duplicates.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(imports)")}
        if "kind" not in cols:
            conn.execute("DROP TABLE imports")
            conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def get_or_create_network(conn: sqlite3.Connection, name: str, label: str | None = None) -> int:
    row = conn.execute("SELECT id, label FROM networks WHERE name=?", (name,)).fetchone()
    if row:
        if label and label != row["label"]:
            conn.execute("UPDATE networks SET label=? WHERE id=?", (label, row["id"]))
            conn.commit()
        return row["id"]
    cur = conn.execute("INSERT INTO networks(name, label) VALUES(?,?)", (name, label or name))
    conn.commit()
    return cur.lastrowid


def get_network(conn: sqlite3.Connection, name: str):
    return conn.execute("SELECT * FROM networks WHERE name=?", (name,)).fetchone()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None
