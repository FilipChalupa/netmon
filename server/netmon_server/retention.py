"""Retention — pruning of high-volume raw measurement rows.

Rows of the high-volume kinds (latency, reach, diag) older than
NETMON_RETENTION_DAYS (default 90; 0 keeps everything) are deleted once
a day in small batches so sync writes are never blocked for long.
speed, pubip and uptime rows are tiny and always kept, and so are the
hourly latency rollups (rollup.py) — long-range charts, summaries and
the heatmap survive the pruning at hourly resolution. SQLite reuses
the freed pages, so the file stops growing; run VACUUM manually if you
also want it to shrink.
"""

from __future__ import annotations

import asyncio
import logging
import time

from .config import ServerConfig
from .db import connect

log = logging.getLogger("netmon.retention")

PRUNE_KINDS = ("latency", "reach", "diag")
BATCH = 50_000


def prune_once(cfg: ServerConfig) -> dict[str, int]:
    cutoff = time.time() - cfg.retention_days * 86400
    deleted: dict[str, int] = {}
    conn = connect(cfg.db_path)
    try:
        for kind in PRUNE_KINDS:
            total = 0
            while True:
                with conn:  # one transaction per batch keeps write locks short
                    n = conn.execute(
                        f"DELETE FROM {kind} WHERE rowid IN "
                        f"(SELECT rowid FROM {kind} WHERE ts_epoch<? LIMIT ?)",
                        (cutoff, BATCH)).rowcount
                total += n
                if n < BATCH:
                    break
                time.sleep(0.1)
            if total:
                deleted[kind] = total
    finally:
        conn.close()
    return deleted


async def retention_loop(cfg: ServerConfig, stop: asyncio.Event) -> None:
    if not cfg.retention_days:
        return
    while not stop.is_set():
        try:
            deleted = await asyncio.to_thread(prune_once, cfg)
            if deleted:
                log.info("pruned rows older than %d days: %s", cfg.retention_days,
                         ", ".join(f"{k} −{v:,}" for k, v in deleted.items()))
        except Exception:
            log.exception("retention pruning failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=24 * 3600)
        except asyncio.TimeoutError:
            pass
