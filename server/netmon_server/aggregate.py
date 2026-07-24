"""Summaries and time series for cards and charts.

Bucketing in SQL: bucket = CAST(ts_epoch/b AS INT)*b where b is a multiple
of 60 s chosen so a series has ≤ ~1500 points (a day = per-minute buckets
like the old version; longer ranges thin themselves out).
"""

from __future__ import annotations

import concurrent.futures
import datetime
import functools
import socket
import sqlite3
from zoneinfo import ZoneInfo

from .events import (PUBLIC_TARGETS, derive_events, derive_reach_events,
                     events_summary, merge_events)

UPTIME_GAP_THRESHOLD = 150  # s; a heartbeat gap longer than this = measuring wasn't running
MAX_POINTS = 1500


def pick_bucket(t0: float, t1: float) -> int:
    span = max(t1 - t0, 60)
    b = int(span / MAX_POINTS // 60 + 1) * 60
    return max(b, 60)


def latency_series(conn: sqlite3.Connection, network_id: int,
                   t0: float, t1: float, bucket: int) -> dict:
    """Latency (avg) and loss (%) per target per bucket. AVG ignores the NULL rtt of LOSS rows."""
    rows = conn.execute(
        "SELECT CAST(ts_epoch/:b AS INT)*:b AS bucket, target, "
        "       AVG(rtt_ms) AS rtt, 100.0*SUM(status='LOSS')/COUNT(*) AS loss "
        "FROM latency WHERE network_id=:net AND ts_epoch>=:t0 AND ts_epoch<=:t1 "
        "GROUP BY bucket, target ORDER BY bucket",
        {"b": bucket, "net": network_id, "t0": t0, "t1": t1},
    ).fetchall()
    buckets: list[int] = []
    index: dict[int, int] = {}
    targets: dict[str, dict] = {}
    for r in rows:
        if r["bucket"] not in index:
            index[r["bucket"]] = len(buckets)
            buckets.append(r["bucket"])
    n = len(buckets)
    for r in rows:
        t = r["target"]
        if t not in targets:
            targets[t] = {"rtt": [None] * n, "loss": [None] * n}
        i = index[r["bucket"]]
        targets[t]["rtt"][i] = round(r["rtt"], 2) if r["rtt"] is not None else None
        targets[t]["loss"][i] = round(r["loss"], 2)
    return {"buckets": buckets, "targets": targets}


def reach_series(conn: sqlite3.Connection, network_id: int,
                 t0: float, t1: float, bucket: int) -> dict:
    rows = conn.execute(
        "SELECT CAST(ts_epoch/:b AS INT)*:b AS bucket, "
        "       AVG(dns_ms) AS dns, AVG(tcp_ms) AS tcp, AVG(tls_ms) AS tls "
        "FROM reach WHERE network_id=:net AND ts_epoch>=:t0 AND ts_epoch<=:t1 AND status='ok' "
        "GROUP BY bucket ORDER BY bucket",
        {"b": bucket, "net": network_id, "t0": t0, "t1": t1},
    ).fetchall()
    fails = conn.execute(
        "SELECT COUNT(*) AS c FROM reach "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? AND status='FAIL'",
        (network_id, t0, t1),
    ).fetchone()["c"]
    return {
        "buckets": [r["bucket"] for r in rows],
        "dns": [round(r["dns"], 1) if r["dns"] is not None else None for r in rows],
        "tcp": [round(r["tcp"], 1) if r["tcp"] is not None else None for r in rows],
        "tls": [round(r["tls"], 1) if r["tls"] is not None else None for r in rows],
        "fails": fails,
    }


def speed_points(conn: sqlite3.Connection, network_id: int,
                 t0: float, t1: float) -> dict:
    """Speed — always raw points (hourly, no bucketing needed)."""
    rows = conn.execute(
        "SELECT ts_epoch, down_mbps, up_mbps FROM speed "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? "
        "AND (down_mbps IS NOT NULL OR up_mbps IS NOT NULL) "
        "ORDER BY ts_epoch",
        (network_id, t0, t1),
    ).fetchall()
    fails = conn.execute(
        "SELECT COUNT(*) AS c FROM speed "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? AND down_mbps IS NULL",
        (network_id, t0, t1),
    ).fetchone()["c"]
    return {
        "ts": [r["ts_epoch"] for r in rows],
        "mbps": [r["down_mbps"] for r in rows],
        "up": [r["up_mbps"] for r in rows],
        "fails": fails,
    }


_ptr_pool = concurrent.futures.ThreadPoolExecutor(2, thread_name_prefix="ptr")


@functools.lru_cache(maxsize=256)
def _ptr(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return None


def ptr_lookup(ip: str, timeout: float = 1.5) -> str | None:
    """Reverse DNS of the public IP — usually names the ISP. Bounded wait so
    a dead resolver can't stall page rendering; results are cached."""
    try:
        return _ptr_pool.submit(_ptr, ip).result(timeout)
    except concurrent.futures.TimeoutError:
        return None


def pubip_history(conn: sqlite3.Connection, network_id: int,
                  t0: float, t1: float) -> dict:
    """Public IP effective at t1 + change records within [t0, t1]."""
    cur = conn.execute(
        "SELECT ts_epoch, ts_iso, ip FROM pubip "
        "WHERE network_id=? AND ts_epoch<=? ORDER BY ts_epoch DESC LIMIT 1",
        (network_id, t1)).fetchone()
    changes = conn.execute(
        "SELECT ts_epoch, ts_iso, ip FROM pubip "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
        (network_id, t0, t1)).fetchall()
    return {
        "current": {
            "ip": cur["ip"],
            "since": cur["ts_iso"],
            "since_epoch": cur["ts_epoch"],
            "ptr": ptr_lookup(cur["ip"]),
        } if cur else None,
        "changes": [{"ts_epoch": r["ts_epoch"], "ts_iso": r["ts_iso"],
                     "ip": r["ip"]} for r in changes],
    }


def attach_diags(conn: sqlite3.Connection, network_id: int,
                 event_dicts: list[dict], t0: float, t1: float) -> list[dict]:
    """Attach traceroute snapshots to the outage events they were captured
    during (small grace after the end — a traceroute takes a while)."""
    rows = conn.execute(
        "SELECT ts_epoch, ts_iso, target, output FROM diag "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
        (network_id, t0 - 60, t1 + 300)).fetchall()
    for e in event_dicts:
        e["diags"] = [
            {"ts_iso": r["ts_iso"], "target": r["target"], "output": r["output"]}
            for r in rows
            if e["start_epoch"] - 60 <= r["ts_epoch"] <= e["end_epoch"] + 300]
    return event_dicts


def daily_heatmap(conn: sqlite3.Connection, network_id: int, tz_name: str,
                  days: int = 365, end_day: datetime.date | None = None,
                  public_targets: tuple[str, ...] = PUBLIC_TARGETS) -> list[dict]:
    """Per-local-day packet loss on the public targets, for the calendar heatmap.

    Aggregates per hour in SQL, then assigns hours to local days in Python —
    hour buckets align with civil time (whole-hour offsets), so day boundaries
    stay DST-correct without fetching millions of raw rows.
    """
    tz = ZoneInfo(tz_name)
    if end_day is None:
        end_day = datetime.datetime.now(tz).date()
    start_day = end_day - datetime.timedelta(days=days - 1)
    t0 = datetime.datetime.combine(start_day, datetime.time.min, tz).timestamp()
    t1 = datetime.datetime.combine(end_day + datetime.timedelta(days=1),
                                   datetime.time.min, tz).timestamp()
    marks = ", ".join("?" * len(public_targets))
    rows = conn.execute(
        f"SELECT CAST(ts_epoch/3600 AS INT)*3600 AS hour, COUNT(*) AS n, "
        f"       SUM(status='LOSS') AS lost "
        f"FROM latency WHERE network_id=? AND ts_epoch>=? AND ts_epoch<? "
        f"AND target IN ({marks}) GROUP BY hour",
        (network_id, t0, t1, *public_targets),
    ).fetchall()
    agg: dict[datetime.date, list[int]] = {}
    for r in rows:
        d = datetime.datetime.fromtimestamp(r["hour"], tz).date()
        a = agg.setdefault(d, [0, 0])
        a[0] += r["n"]
        a[1] += r["lost"]
    out = []
    d = start_day
    while d <= end_day:
        n, lost = agg.get(d, (0, 0))
        out.append({"day": d.isoformat(), "samples": n,
                    "loss": round(100.0 * lost / n, 2) if n else None})
        d += datetime.timedelta(days=1)
    return out


def uptime_panel(conn: sqlite3.Connection, network_id: int,
                 t0: float, t1: float) -> dict:
    """Measurement coverage from heartbeats: gap >150 s = not running; STOP
    before the gap = controlled shutdown, otherwise crash/powered-off host."""
    rows = conn.execute(
        "SELECT ts_epoch, ts_iso, event FROM uptime "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
        (network_id, t0, t1),
    ).fetchall()
    if not rows:
        return {"first": None, "last": None, "span_s": 0, "down_s": 0,
                "coverage": None, "gaps": []}
    gaps = []
    down = 0.0
    prev = rows[0]
    for r in rows[1:]:
        gap = r["ts_epoch"] - prev["ts_epoch"]
        if gap > UPTIME_GAP_THRESHOLD:
            cause = "stopped" if prev["event"] == "STOP" else "crash"
            gaps.append({"from": prev["ts_iso"], "to": r["ts_iso"],
                         "dur": int(gap), "cause": cause})
            down += gap
        prev = r
    span = rows[-1]["ts_epoch"] - rows[0]["ts_epoch"]
    coverage = (span - down) / span * 100 if span > 0 else 100.0
    return {
        "first": rows[0]["ts_iso"],
        "last": rows[-1]["ts_iso"],
        "span_s": int(span),
        "down_s": int(down),
        "coverage": round(coverage, 1),
        "gaps": gaps,
    }


def summary(conn: sqlite3.Connection, network_id: int,
            t0: float, t1: float, ping_interval: float = 2.0,
            reach_min_fails: int = 10) -> dict:
    """Summary for cards: per-target samples/loss/latency, speed, coverage, outages."""
    target_rows = conn.execute(
        "SELECT target, COUNT(*) AS samples, "
        "       100.0*SUM(status='LOSS')/COUNT(*) AS loss, "
        "       AVG(rtt_ms) AS avg, MIN(rtt_ms) AS min, MAX(rtt_ms) AS max "
        "FROM latency WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? "
        "GROUP BY target ORDER BY target",
        (network_id, t0, t1),
    ).fetchall()
    targets = [{
        "target": r["target"],
        "samples": r["samples"],
        "loss": round(r["loss"], 2),
        "avg": round(r["avg"], 2) if r["avg"] is not None else None,
        "min": round(r["min"], 2) if r["min"] is not None else None,
        "max": round(r["max"], 2) if r["max"] is not None else None,
    } for r in target_rows]

    spd = conn.execute(
        "SELECT COUNT(*) AS n, AVG(down_mbps) AS avg, MIN(down_mbps) AS min, "
        "       MAX(down_mbps) AS max, "
        "       AVG(up_mbps) AS up_avg, MIN(up_mbps) AS up_min, "
        "       MAX(up_mbps) AS up_max "
        "FROM speed WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? "
        "AND down_mbps IS NOT NULL",
        (network_id, t0, t1),
    ).fetchone()
    last_spd = conn.execute(
        "SELECT down_mbps, up_mbps, ts_iso FROM speed "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? AND down_mbps IS NOT NULL "
        "ORDER BY ts_epoch DESC LIMIT 1",
        (network_id, t0, t1),
    ).fetchone()

    events = merge_events(
        derive_events(conn, network_id, t0, t1, ping_interval),
        derive_reach_events(conn, network_id, t0, t1, reach_min_fails))

    meta = conn.execute(
        "SELECT MIN(ts_epoch) AS first, MAX(ts_epoch) AS last, "
        "       MIN(ts_iso) AS first_iso, MAX(ts_iso) AS last_iso "
        "FROM latency WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=?",
        (network_id, t0, t1),
    ).fetchone()

    return {
        "targets": targets,
        "speed": {
            "n": spd["n"],
            "avg": round(spd["avg"], 1) if spd["avg"] is not None else None,
            "min": round(spd["min"], 1) if spd["min"] is not None else None,
            "max": round(spd["max"], 1) if spd["max"] is not None else None,
            "up_avg": round(spd["up_avg"], 1) if spd["up_avg"] is not None else None,
            "up_min": round(spd["up_min"], 1) if spd["up_min"] is not None else None,
            "up_max": round(spd["up_max"], 1) if spd["up_max"] is not None else None,
            "last": last_spd["down_mbps"] if last_spd else None,
            "up_last": last_spd["up_mbps"] if last_spd else None,
            "last_at": last_spd["ts_iso"] if last_spd else None,
        },
        "uptime": uptime_panel(conn, network_id, t0, t1),
        "pubip": pubip_history(conn, network_id, t0, t1),
        "events": attach_diags(conn, network_id,
                               [e.as_dict() for e in events], t0, t1),
        "events_summary": events_summary(events),
        "period": {"first": meta["first_iso"], "last": meta["last_iso"]},
    }
