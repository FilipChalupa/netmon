"""One-off import of historical CSVs from the bash version (log/YYYYMMDD/*.csv).

Usage:
    python -m netmon_server.importer --network home --label "Home" /path/to/log

File-level idempotency: the imports table (network_id, path, sha256) — an
already-imported file is skipped. --force deletes previously imported rows of
the given day (src_id IS NULL) and re-imports. Rows synced from monitors
(src_id NOT NULL) are never touched by --force.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import os
import sqlite3
import sys
import time

from .config import load_config
from .db import connect, get_or_create_network, init_db, insert_sql

CSV_NAMES = ("latency.csv", "reach.csv", "speed.csv", "uptime.csv")


def _epoch(ts_iso: str) -> float:
    return datetime.datetime.fromisoformat(ts_iso).timestamp()


def _num(s: str) -> float | None:
    return float(s) if s not in ("", None) else None


def _int(s: str) -> int | None:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _code(s: str) -> int:
    """http_code from old CSVs: '200' → 200, 'FAIL'/'000'/'' → 0."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def parse_rows(kind: str, path: str, network_id: int):
    """Yields tuples for insert_sql(kind); src_id = NULL."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return
        for row in reader:
            if not row or not row[0]:
                continue
            ts_iso = row[0]
            if kind == "latency":
                # timestamp,target,ip,status,rtt_ms; drop sentinel STOPPED rows
                if len(row) < 5 or row[1] == "--":
                    continue
                yield (network_id, None, _epoch(ts_iso), ts_iso,
                       row[1], row[2], row[3], _num(row[4]))
            elif kind == "reach":
                # timestamp,dns_ms,tcp_ms,tls_ms,http_code,status
                if len(row) < 6:
                    continue
                yield (network_id, None, _epoch(ts_iso), ts_iso,
                       _num(row[1]), _num(row[2]), _num(row[3]), _code(row[4]), row[5])
            elif kind == "speed":
                # timestamp,down_mbps,bytes,seconds,http_code[,up_mbps]
                # (legacy bash CSVs have no upload column)
                if len(row) < 5:
                    continue
                yield (network_id, None, _epoch(ts_iso), ts_iso,
                       _num(row[1]), _int(row[2]), _num(row[3]), _code(row[4]),
                       _num(row[5]) if len(row) > 5 else None)
            elif kind == "uptime":
                # timestamp,event
                if len(row) < 2:
                    continue
                yield (network_id, None, _epoch(ts_iso), ts_iso, row[1])


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def import_file(conn: sqlite3.Connection, network_id: int, kind: str,
                path: str, day: str, force: bool) -> int | None:
    """Returns the number of inserted rows, None = skipped (already imported).

    Deduplication is by content (network, kind, day, sha256), not by path —
    web uploads extract into a fresh temp directory every time, so paths
    never repeat. Re-importing a day first removes its previously imported
    rows (src_id IS NULL), so changed files replace instead of duplicating.
    """
    digest = _sha256(path)
    prev = conn.execute(
        "SELECT sha256 FROM imports WHERE network_id=? AND kind=? AND day=?",
        (network_id, kind, day)).fetchone()
    if prev and not force and prev["sha256"] == digest:
        return None

    day_prefix = f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
    with conn:
        if prev or force:
            conn.execute(
                f"DELETE FROM {kind} WHERE network_id=? AND src_id IS NULL "
                f"AND substr(ts_iso,1,10)=?", (network_id, day_prefix))
        cur = conn.executemany(insert_sql(kind), parse_rows(kind, path, network_id))
        inserted = cur.rowcount
        conn.execute(
            "INSERT INTO imports(network_id, kind, day, sha256, path, imported_at) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(network_id, kind, day) DO UPDATE SET sha256=excluded.sha256, "
            "path=excluded.path, imported_at=excluded.imported_at",
            (network_id, kind, day, digest, os.path.abspath(path), time.time()))
    return inserted


def import_tree(conn: sqlite3.Connection, network_id: int, log_root: str,
                force: bool, log=print) -> dict:
    stats = {"days": 0, "files": 0, "skipped": 0, "rows": 0}
    days = sorted(d for d in os.listdir(log_root)
                  if len(d) == 8 and d.isdigit()
                  and os.path.isdir(os.path.join(log_root, d)))
    for day in days:
        day_rows = 0
        for name in CSV_NAMES:
            path = os.path.join(log_root, day, name)
            if not os.path.isfile(path):
                continue
            kind = name.removesuffix(".csv")
            n = import_file(conn, network_id, kind, path, day, force)
            if n is None:
                stats["skipped"] += 1
            else:
                stats["files"] += 1
                stats["rows"] += n
                day_rows += n
        stats["days"] += 1
        log(f"  {day}: +{day_rows} rows")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Import historical netmon CSVs")
    ap.add_argument("log_root", help="log/ directory containing YYYYMMDD subdirectories")
    ap.add_argument("--network", required=True, help="network name (tag in the database)")
    ap.add_argument("--label", help="human-readable network label (default = name)")
    ap.add_argument("--force", action="store_true", help="overwrite already imported files")
    args = ap.parse_args()

    if not os.path.isdir(args.log_root):
        print(f"Directory not found: {args.log_root}", file=sys.stderr)
        return 1

    cfg = load_config()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        network_id = get_or_create_network(conn, args.network, args.label)
        print(f"Importing into network '{args.network}' (DB {cfg.db_path}) from {args.log_root}:")
        stats = import_tree(conn, network_id, args.log_root, args.force)
        print(f"Done: {stats['days']} days, {stats['files']} files, "
              f"{stats['rows']} rows (+{stats['skipped']} skipped).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
