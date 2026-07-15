"""Local SQLite storage for measurements.

One writer connection guarded by a lock (probe threads); HTTP handlers read
through their own short-lived connections — WAL mode allows concurrent reads.

AUTOINCREMENT is deliberate: retention deletes must never recycle row ids,
otherwise the evaluation server's sync cursors (after_id) would break.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS latency(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    target TEXT NOT NULL,
    ip TEXT,
    status TEXT NOT NULL,
    rtt_ms REAL
);
CREATE INDEX IF NOT EXISTS idx_latency_ts ON latency(ts_epoch);

CREATE TABLE IF NOT EXISTS reach(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    dns_ms REAL,
    tcp_ms REAL,
    tls_ms REAL,
    http_code INTEGER,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reach_ts ON reach(ts_epoch);

CREATE TABLE IF NOT EXISTS speed(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    down_mbps REAL,
    bytes INTEGER,
    seconds REAL,
    http_code INTEGER
);
CREATE INDEX IF NOT EXISTS idx_speed_ts ON speed(ts_epoch);

CREATE TABLE IF NOT EXISTS uptime(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    event TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uptime_ts ON uptime(ts_epoch);
"""

# columns returned by the API, in JSON row order
KIND_COLUMNS = {
    "latency": ["id", "ts_epoch", "ts_iso", "target", "ip", "status", "rtt_ms"],
    "reach": ["id", "ts_epoch", "ts_iso", "dns_ms", "tcp_ms", "tls_ms", "http_code", "status"],
    "speed": ["id", "ts_epoch", "ts_iso", "down_mbps", "bytes", "seconds", "http_code"],
    "uptime": ["id", "ts_epoch", "ts_iso", "event"],
}


class Db:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def insert_latency(self, ts_epoch, ts_iso, target, ip, status, rtt_ms):
        self._write(
            "INSERT INTO latency(ts_epoch, ts_iso, target, ip, status, rtt_ms) VALUES(?,?,?,?,?,?)",
            (ts_epoch, ts_iso, target, ip, status, rtt_ms),
        )

    def insert_reach(self, ts_epoch, ts_iso, dns_ms, tcp_ms, tls_ms, http_code, status):
        self._write(
            "INSERT INTO reach(ts_epoch, ts_iso, dns_ms, tcp_ms, tls_ms, http_code, status) VALUES(?,?,?,?,?,?,?)",
            (ts_epoch, ts_iso, dns_ms, tcp_ms, tls_ms, http_code, status),
        )

    def insert_speed(self, ts_epoch, ts_iso, down_mbps, bytes_, seconds, http_code):
        self._write(
            "INSERT INTO speed(ts_epoch, ts_iso, down_mbps, bytes, seconds, http_code) VALUES(?,?,?,?,?,?)",
            (ts_epoch, ts_iso, down_mbps, bytes_, seconds, http_code),
        )

    def insert_uptime(self, ts_epoch, ts_iso, event):
        self._write(
            "INSERT INTO uptime(ts_epoch, ts_iso, event) VALUES(?,?,?)",
            (ts_epoch, ts_iso, event),
        )

    def purge(self, retention_days: int) -> int:
        """Delete records older than retention_days. Returns deleted row count."""
        cutoff = time.time() - retention_days * 86400
        deleted = 0
        with self._lock:
            for table in KIND_COLUMNS:
                cur = self._conn.execute(f"DELETE FROM {table} WHERE ts_epoch < ?", (cutoff,))
                deleted += cur.rowcount
            self._conn.commit()
        return deleted

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def fetch_after(db_path: str, kind: str, after_id: int, limit: int) -> tuple[list[dict], bool]:
    """Read path for the HTTP API — own short-lived connection (HTTP threads).

    Returns (rows, more): fetching limit+1 rows reveals whether more exist.
    """
    cols = KIND_COLUMNS[kind]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.execute(
            f"SELECT {', '.join(cols)} FROM {kind} WHERE id > ? ORDER BY id LIMIT ?",
            (after_id, limit + 1),
        )
        raw = cur.fetchall()
    finally:
        conn.close()
    more = len(raw) > limit
    rows = [dict(zip(cols, r)) for r in raw[:limit]]
    return rows, more
