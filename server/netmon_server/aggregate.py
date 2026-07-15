"""Souhrny a časové řady pro karty a grafy.

Bucketování v SQL: bucket = CAST(ts_epoch/b AS INT)*b, kde b je násobek 60 s
zvolený tak, aby řada měla ≤ ~1500 bodů (den = minutové buckety jako ve staré
verzi, delší rozsahy se samy zředí).
"""

from __future__ import annotations

import sqlite3

from .events import derive_events, events_summary

UPTIME_GAP_THRESHOLD = 150  # s; mezera mezi tepy delší než tohle = měření neběželo
MAX_POINTS = 1500


def pick_bucket(t0: float, t1: float) -> int:
    span = max(t1 - t0, 60)
    b = int(span / MAX_POINTS // 60 + 1) * 60
    return max(b, 60)


def latency_series(conn: sqlite3.Connection, network_id: int,
                   t0: float, t1: float, bucket: int) -> dict:
    """Latence (avg) a ztráty (%) per cíl per bucket. AVG ignoruje NULL rtt LOSS řádků."""
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
    """Rychlost — vždy surové body (1×/h, bucketovat netřeba)."""
    rows = conn.execute(
        "SELECT ts_epoch, down_mbps FROM speed "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? AND down_mbps IS NOT NULL "
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
        "fails": fails,
    }


def uptime_panel(conn: sqlite3.Connection, network_id: int,
                 t0: float, t1: float) -> dict:
    """Pokrytí měření z tepů: mezera >150 s = neběželo; STOP před mezerou
    = řízené zastavení, jinak pád/vypnutý počítač."""
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
            t0: float, t1: float, ping_interval: float = 2.0) -> dict:
    """Souhrn pro karty: per cíl vzorky/ztráta/latence, rychlost, pokrytí, výpadky."""
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
        "       MAX(down_mbps) AS max "
        "FROM speed WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? "
        "AND down_mbps IS NOT NULL",
        (network_id, t0, t1),
    ).fetchone()
    last_spd = conn.execute(
        "SELECT down_mbps, ts_iso FROM speed "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? AND down_mbps IS NOT NULL "
        "ORDER BY ts_epoch DESC LIMIT 1",
        (network_id, t0, t1),
    ).fetchone()

    events = derive_events(conn, network_id, t0, t1, ping_interval)

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
            "last": last_spd["down_mbps"] if last_spd else None,
            "last_at": last_spd["ts_iso"] if last_spd else None,
        },
        "uptime": uptime_panel(conn, network_id, t0, t1),
        "events": [e.as_dict() for e in events],
        "events_summary": events_summary(events),
        "period": {"first": meta["first_iso"], "last": meta["last_iso"]},
    }
