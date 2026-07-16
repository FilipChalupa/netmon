"""All-in-one mode: measuring monitor + evaluation web server in one process.

    python -m netmon_server.single [--config monitor.ini] [--port 8000]

The embedded evaluation server pulls from the local monitor's mini API over
localhost through exactly the same sync path it would use over Tailscale —
no special cases. Data lives in two SQLite files under --data-dir (monitor
buffer + server DB), and the monitor API stays reachable, so a central
evaluation server can still pull from this instance later.

This is what the released single binaries run by default.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from netmon_monitor import VERSION as MONITOR_VERSION
from netmon_monitor.config import Config as MonitorConfig
from netmon_monitor.config import load_config as load_monitor_config
from netmon_monitor.db import Db
from netmon_monitor.server import create_server
from netmon_monitor.workers import now_iso, start_workers

from . import VERSION
from .config import MonitorCfg, ServerConfig, set_config_override

DEFAULT_DATA_DIR = "~/.local/share/netmon"


def build_server_config(mcfg: MonitorConfig, data_dir: str, tz: str) -> ServerConfig:
    """Evaluation-server config pointing at the local monitor."""
    return ServerConfig(
        db_path=os.path.join(data_dir, "server.db"),
        monitors_path="",
        tz=tz,
        sync_interval=10.0,  # localhost pull is cheap — keep the UI snappy
        ping_interval=mcfg.ping_interval,
        monitors=[MonitorCfg(name=mcfg.network, url=f"http://127.0.0.1:{mcfg.port}",
                             label=mcfg.network, token=mcfg.token)],
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="netmon single",
        description="netmon all-in-one: monitor this machine's network and "
                    "serve the results web UI")
    ap.add_argument("--config", default="~/.config/netmon/monitor.ini",
                    help="monitor config (optional; defaults apply when missing)")
    ap.add_argument("--network", default=None,
                    help="network name shown in the UI (overrides the config)")
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR,
                    help=f"where the SQLite files live (default: {DEFAULT_DATA_DIR})")
    ap.add_argument("--host", default="0.0.0.0", help="web UI bind address")
    ap.add_argument("--port", type=int, default=8000, help="web UI port")
    ap.add_argument("--tz", default=os.environ.get("NETMON_TZ", ServerConfig.tz),
                    help="timezone for days/reports")
    args = ap.parse_args(argv)

    config_path = os.path.expanduser(args.config)
    data_dir = os.path.expanduser(args.data_dir)
    if os.path.exists(config_path):
        mcfg = load_monitor_config(config_path)
    else:
        mcfg = MonitorConfig(db_path=os.path.join(data_dir, "monitor.db"))
    if args.network:
        mcfg.network = args.network

    set_config_override(build_server_config(mcfg, data_dir, args.tz))
    import uvicorn  # after the override, before the app import

    from .main import app

    # --- monitor half: probe threads + mini API on cfg.port ---
    stop = threading.Event()
    started_at = now_iso()
    db = Db(mcfg.resolved_db_path())
    db.insert_uptime(time.time(), started_at, "START")
    httpd = create_server(mcfg, mcfg.resolved_db_path(), started_at)
    threading.Thread(target=httpd.serve_forever, name="monitor-http",
                     daemon=True).start()
    threads = start_workers(mcfg, db, stop)

    print(f"netmon single v{VERSION} (monitor v{MONITOR_VERSION}) — network "
          f"'{mcfg.network}', data in {data_dir}", flush=True)
    print(f"web UI: http://{args.host}:{args.port}  ·  monitor API: "
          f":{mcfg.port}", flush=True)

    # --- server half: uvicorn owns the main thread and the signals; when it
    # returns (SIGINT/SIGTERM), shut the monitor down cleanly ---
    # uvicorn captures the signal during run() and re-raises it afterwards with
    # the pre-run handlers restored; these no-ops absorb that replay so the
    # default handler can't kill the process before the cleanup below runs.
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: None)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        stop.set()
        httpd.shutdown()
        for t in threads:
            t.join(timeout=10)
        db.insert_uptime(time.time(), now_iso(), "STOP")
        db.close()
        print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
