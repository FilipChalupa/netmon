"""Sync worker — continuous pulling of data from monitors (over Tailscale).

Paginates /api/data/{kind}?after_id=N; each page is stored in a single
transaction together with the cursor advance (crash-safe). Duplicates are
prevented by UNIQUE(network_id, src_id) + INSERT OR IGNORE.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

import httpx

from .config import MonitorCfg, ServerConfig
from .db import KINDS, connect, get_or_create_network, insert_sql

log = logging.getLogger("netmon.sync")

PAGE_LIMIT = 5000

# mapping of a monitor JSON row → column order for insert_sql(kind)
ROW_FIELDS = {
    "latency": ("target", "ip", "status", "rtt_ms"),
    "reach": ("dns_ms", "tcp_ms", "tls_ms", "http_code", "status"),
    "speed": ("down_mbps", "bytes", "seconds", "http_code"),
    "uptime": ("event",),
    "pubip": ("ip",),
    "diag": ("target", "output"),
}


def _headers(mon: MonitorCfg) -> dict:
    return {"X-Netmon-Token": mon.token} if mon.token else {}


def _store_page(conn: sqlite3.Connection, kind: str, network_id: int,
                rows: list[dict], last_id: int) -> int:
    params = [
        tuple([network_id, r["id"], r["ts_epoch"], r["ts_iso"]]
              + [r.get(f) for f in ROW_FIELDS[kind]])
        for r in rows
    ]
    with conn:  # single transaction: data + cursor
        cur = conn.executemany(insert_sql(kind), params)
        inserted = cur.rowcount
        conn.execute(
            "INSERT INTO sync_cursor(network_id, kind, last_src_id, updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(network_id, kind) DO UPDATE SET last_src_id=excluded.last_src_id, "
            "updated_at=excluded.updated_at",
            (network_id, kind, last_id, time.time()),
        )
    return inserted


async def pull_monitor(conn: sqlite3.Connection, client: httpx.AsyncClient,
                       mon: MonitorCfg) -> int:
    """Pull all new data from one monitor. Returns the number of new rows."""
    network_id = get_or_create_network(conn, mon.name, mon.label)
    total = 0
    for kind in KINDS:
        row = conn.execute(
            "SELECT last_src_id FROM sync_cursor WHERE network_id=? AND kind=?",
            (network_id, kind),
        ).fetchone()
        after_id = row["last_src_id"] if row else 0
        while True:
            resp = await client.get(
                f"{mon.url}/api/data/{kind}",
                params={"after_id": after_id, "limit": PAGE_LIMIT},
                headers=_headers(mon),
            )
            if resp.status_code == 404:
                break  # monitor predating this data kind — skip, keep syncing the rest
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("rows", [])
            if not rows:
                break
            last_id = payload["last_id"]
            total += await asyncio.to_thread(
                _store_page, conn, kind, network_id, rows, last_id
            )
            after_id = last_id
            if not payload.get("more"):
                break
    return total


def _mark_status(conn: sqlite3.Connection, network_id: int, error: str | None) -> None:
    now = time.time()
    if error is None:
        conn.execute(
            "INSERT INTO sync_status(network_id, last_ok_at, last_attempt_at, last_error, consecutive_failures) "
            "VALUES(?,?,?,NULL,0) "
            "ON CONFLICT(network_id) DO UPDATE SET last_ok_at=excluded.last_ok_at, "
            "last_attempt_at=excluded.last_attempt_at, last_error=NULL, consecutive_failures=0",
            (network_id, now, now),
        )
    else:
        conn.execute(
            "INSERT INTO sync_status(network_id, last_attempt_at, last_error, consecutive_failures) "
            "VALUES(?,?,?,1) "
            "ON CONFLICT(network_id) DO UPDATE SET last_attempt_at=excluded.last_attempt_at, "
            "last_error=excluded.last_error, consecutive_failures=consecutive_failures+1",
            (network_id, now, error[:500]),
        )
    conn.commit()


async def sync_once(conn: sqlite3.Connection, client: httpx.AsyncClient,
                    cfg: ServerConfig) -> None:
    for mon in cfg.monitors:
        network_id = get_or_create_network(conn, mon.name, mon.label)
        try:
            n = await pull_monitor(conn, client, mon)
            _mark_status(conn, network_id, None)
            if n:
                log.info("sync %s: +%d rows", mon.name, n)
        except Exception as e:  # an unreachable monitor must not kill the loop
            _mark_status(conn, network_id, f"{type(e).__name__}: {e}")
            log.warning("sync %s failed: %s", mon.name, e)


async def sync_forever(cfg: ServerConfig, stop: asyncio.Event) -> None:
    conn = connect(cfg.db_path)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            while not stop.is_set():
                await sync_once(conn, client, cfg)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=cfg.sync_interval)
                except asyncio.TimeoutError:
                    pass
    finally:
        conn.close()
