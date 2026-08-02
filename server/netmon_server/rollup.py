"""Hourly latency rollups — precomputed aggregates for long-range views.

Raw pings arrive seconds apart, so a quarter/year chart or the 12-month
heatmap over raw rows means scanning millions of them per request. The
latency_hourly table keeps per-(network, target, hour) sums that answer
the same questions from a few thousand rows instead. Unlike raw rows it
is never pruned by retention, so long-range history stays available at
hourly resolution after NETMON_RETENTION_DAYS cuts the raw data.

Freshness: sync calls update_hours() for the span each pull touched
(delete + reinsert of whole hours — idempotent, backfill-safe). At
startup backfill_all() rebuilds any network whose rollup isn't marked
ready; until then aggregates fall back to raw scans.
"""

from __future__ import annotations

import logging
import sqlite3
import time

from .config import ServerConfig
from .db import connect

log = logging.getLogger("netmon.rollup")

HOUR = 3600
REBUILD_CHUNK_S = 7 * 86400  # one transaction per week of raw data


def update_hours(conn: sqlite3.Connection, network_id: int,
                 t_from: float, t_to: float) -> None:
    """Recompute every hour intersecting [t_from, t_to] from raw rows."""
    h0 = int(t_from // HOUR) * HOUR
    h1 = int(t_to // HOUR) * HOUR + HOUR
    with conn:
        conn.execute(
            "DELETE FROM latency_hourly WHERE network_id=? AND hour>=? AND hour<?",
            (network_id, h0, h1))
        conn.execute(
            "INSERT INTO latency_hourly"
            "(network_id, target, hour, samples, lost, rtt_n, rtt_sum, rtt_min, rtt_max) "
            "SELECT network_id, target, CAST(ts_epoch/3600 AS INT)*3600, "
            "       COUNT(*), SUM(status='LOSS'), COUNT(rtt_ms), "
            "       SUM(rtt_ms), MIN(rtt_ms), MAX(rtt_ms) "
            "FROM latency WHERE network_id=? AND ts_epoch>=? AND ts_epoch<? "
            "GROUP BY target, CAST(ts_epoch/3600 AS INT)*3600",
            (network_id, h0, h1))


def is_ready(conn: sqlite3.Connection, network_id: int) -> bool:
    row = conn.execute("SELECT ready FROM rollup_state WHERE network_id=?",
                       (network_id,)).fetchone()
    return bool(row and row["ready"])


def _set_ready(conn: sqlite3.Connection, network_id: int, ready: bool) -> None:
    conn.execute(
        "INSERT INTO rollup_state(network_id, ready, updated_at) VALUES(?,?,?) "
        "ON CONFLICT(network_id) DO UPDATE SET ready=excluded.ready, "
        "updated_at=excluded.updated_at",
        (network_id, int(ready), time.time()))
    conn.commit()


def rebuild_network(conn: sqlite3.Connection, network_id: int) -> None:
    """Full rebuild in week-sized chunks so no single write lock gets long."""
    _set_ready(conn, network_id, False)
    span = conn.execute(
        "SELECT MIN(ts_epoch) AS lo, MAX(ts_epoch) AS hi FROM latency "
        "WHERE network_id=?", (network_id,)).fetchone()
    if span["lo"] is not None:
        t = span["lo"]
        while t <= span["hi"]:
            update_hours(conn, network_id, t, min(t + REBUILD_CHUNK_S, span["hi"]))
            t += REBUILD_CHUNK_S
    _set_ready(conn, network_id, True)


def backfill_all(cfg: ServerConfig) -> None:
    """Startup task: rebuild rollups for networks not yet marked ready."""
    conn = connect(cfg.db_path)
    try:
        for net in conn.execute("SELECT id, name FROM networks").fetchall():
            if is_ready(conn, net["id"]):
                continue
            t0 = time.time()
            rebuild_network(conn, net["id"])
            log.info("rollup backfill for %s done in %.1f s",
                     net["name"], time.time() - t0)
    except Exception:
        log.exception("rollup backfill failed")
    finally:
        conn.close()
