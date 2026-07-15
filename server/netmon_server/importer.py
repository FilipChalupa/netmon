"""Jednorázový import historických CSV z bashové verze (log/RRRRMMDD/*.csv).

Použití:
    python -m netmon_server.importer --network doma --label "Doma" /cesta/k/log

Idempotence na úrovni souborů: tabulka imports (network_id, path, sha256) —
už naimportovaný soubor se přeskočí. --force smaže dřívější importované řádky
daného dne (src_id IS NULL) a naimportuje znovu. Řádky ze syncu monitorů
(src_id NOT NULL) se --force nedotkne.
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
    """http_code ze starých CSV: '200' → 200, 'FAIL'/'000'/'' → 0."""
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0


def parse_rows(kind: str, path: str, network_id: int):
    """Generuje tuple pro insert_sql(kind); src_id = NULL."""
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
                # timestamp,target,ip,status,rtt_ms; sentinel STOPPED řádky pryč
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
                # timestamp,down_mbps,bytes,seconds,http_code
                if len(row) < 5:
                    continue
                yield (network_id, None, _epoch(ts_iso), ts_iso,
                       _num(row[1]), _int(row[2]), _num(row[3]), _code(row[4]))
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
    """Vrací počet vložených řádků, None = přeskočeno (už naimportováno)."""
    key = os.path.abspath(path)
    digest = _sha256(path)
    existing = conn.execute(
        "SELECT sha256 FROM imports WHERE network_id=? AND path=?",
        (network_id, key)).fetchone()
    if existing and not force and existing["sha256"] == digest:
        return None

    day_prefix = f"{day[0:4]}-{day[4:6]}-{day[6:8]}"
    with conn:
        if existing or force:
            # znovu-import: pryč s dříve importovanými řádky daného dne
            conn.execute(
                f"DELETE FROM {kind} WHERE network_id=? AND src_id IS NULL "
                f"AND substr(ts_iso,1,10)=?", (network_id, day_prefix))
        cur = conn.executemany(insert_sql(kind), parse_rows(kind, path, network_id))
        inserted = cur.rowcount
        conn.execute(
            "INSERT INTO imports(network_id, path, sha256, imported_at) VALUES(?,?,?,?) "
            "ON CONFLICT(network_id, path) DO UPDATE SET sha256=excluded.sha256, "
            "imported_at=excluded.imported_at",
            (network_id, key, digest, time.time()))
    return inserted


def import_tree(conn: sqlite3.Connection, network_id: int, log_root: str,
                force: bool) -> dict:
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
        print(f"  {day}: +{day_rows} řádků")
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="Import historických netmon CSV")
    ap.add_argument("log_root", help="adresář log/ se složkami RRRRMMDD")
    ap.add_argument("--network", required=True, help="jméno sítě (tag v databázi)")
    ap.add_argument("--label", help="lidský popisek sítě (výchozí = jméno)")
    ap.add_argument("--force", action="store_true", help="přepsat už naimportované soubory")
    args = ap.parse_args()

    if not os.path.isdir(args.log_root):
        print(f"Adresář nenalezen: {args.log_root}", file=sys.stderr)
        return 1

    cfg = load_config()
    init_db(cfg.db_path)
    conn = connect(cfg.db_path)
    try:
        network_id = get_or_create_network(conn, args.network, args.label)
        print(f"Import do sítě '{args.network}' (DB {cfg.db_path}) z {args.log_root}:")
        stats = import_tree(conn, network_id, args.log_root, args.force)
        print(f"Hotovo: {stats['days']} dnů, {stats['files']} souborů, "
              f"{stats['rows']} řádků (+{stats['skipped']} přeskočeno).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
