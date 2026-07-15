"""Vstupní bod: python3 -m netmon_monitor --config ~/.config/netmon/monitor.ini"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from . import VERSION
from .config import load_config
from .db import Db
from .server import create_server
from .workers import now_iso, start_workers


def main() -> int:
    ap = argparse.ArgumentParser(prog="netmon_monitor", description="netmon měřicí agent")
    ap.add_argument("--config", default="~/.config/netmon/monitor.ini",
                    help="cesta ke konfiguraci (výchozí: ~/.config/netmon/monitor.ini)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db = Db(cfg.resolved_db_path())

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    started_at = now_iso()
    db.insert_uptime(time.time(), started_at, "START")
    print(f"netmon monitor v{VERSION} — síť '{cfg.network}', DB {cfg.resolved_db_path()}, "
          f"API na {cfg.bind}:{cfg.port}", flush=True)

    httpd = create_server(cfg, cfg.resolved_db_path(), started_at)
    http_thread = threading.Thread(target=httpd.serve_forever, name="http")
    http_thread.start()

    threads = start_workers(cfg, db, stop)

    stop.wait()
    print("Zastavuji…", flush=True)
    httpd.shutdown()
    http_thread.join()
    for t in threads:
        t.join(timeout=10)
    db.insert_uptime(time.time(), now_iso(), "STOP")
    db.close()
    print("Zastaveno.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
