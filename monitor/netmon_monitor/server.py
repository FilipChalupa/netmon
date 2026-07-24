"""Mini HTTP API for the evaluation server (pull over Tailscale).

Endpoints:
  GET  /api/health                          → {status, time, started_at}
  GET  /api/info                            → {network, version, hostname, targets, intervals}
  GET  /api/data/{kind}?after_id=N&limit=M  → {kind, rows, last_id, more}
  POST /api/run/speed                       → 202 started | 409 busy
                                              (on-demand test, result lands in
                                              the speed table like any other)

Authentication: X-Netmon-Token header (only when a token is configured).
"""

from __future__ import annotations

import hmac
import http.server
import json
import socket
import sqlite3
import threading
import time
import urllib.parse

from . import VERSION
from .config import Config
from .db import KIND_COLUMNS, fetch_after
from .workers import measure_speed, now_iso

# only one on-demand speed test at a time — a second request gets 409
_ondemand_speed = threading.Lock()


def run_speed_test_async(cfg: Config, db_path: str) -> bool:
    """Kick off a speed test in a background thread; False when one is
    already running. Uses its own SQLite connection (WAL + busy timeout),
    so it doesn't need the workers' writer instance."""
    if not _ondemand_speed.acquire(blocking=False):
        return False

    def run():
        try:
            mbps, bytes_, seconds, code, up_mbps, idle, loaded = \
                measure_speed(cfg, threading.Event())
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute(
                    "INSERT INTO speed(ts_epoch, ts_iso, down_mbps, bytes, "
                    "seconds, http_code, up_mbps, idle_rtt_ms, loaded_rtt_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (time.time(), now_iso(), mbps, bytes_, seconds, code,
                     up_mbps, idle, loaded))
                conn.commit()
            finally:
                conn.close()
        finally:
            _ondemand_speed.release()

    threading.Thread(target=run, daemon=True, name="speed-ondemand").start()
    return True

MAX_LIMIT = 10000
DEFAULT_LIMIT = 5000


def make_handler(cfg: Config, db_path: str, started_at: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = f"netmon-monitor/{VERSION}"

        def log_message(self, fmt, *args):  # keep the journal quiet; errors show as status codes
            pass

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check_token(self) -> bool:
            if not cfg.token:
                return True
            supplied = self.headers.get("X-Netmon-Token", "")
            return hmac.compare_digest(supplied, cfg.token)

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/")

            if not self._check_token():
                self._send_json(401, {"error": "invalid token"})
                return

            if path == "/api/health":
                self._send_json(200, {"status": "ok", "time": now_iso(), "started_at": started_at})
            elif path == "/api/info":
                self._send_json(200, {
                    "network": cfg.network,
                    "version": VERSION,
                    "hostname": socket.gethostname(),
                    "targets": [[n, ip] for n, ip in cfg.targets],
                    "intervals": {
                        "ping": cfg.ping_interval,
                        "reach": cfg.reach_interval,
                        "speed": cfg.speed_interval,
                        "heartbeat": cfg.heartbeat_interval,
                        "pubip": cfg.pubip_interval,
                    },
                })
            elif path.startswith("/api/data/"):
                kind = path.removeprefix("/api/data/")
                if kind not in KIND_COLUMNS:
                    self._send_json(404, {"error": f"unknown data kind: {kind}"})
                    return
                q = urllib.parse.parse_qs(parsed.query)
                try:
                    after_id = int(q.get("after_id", ["0"])[0])
                    limit = int(q.get("limit", [str(DEFAULT_LIMIT)])[0])
                except ValueError:
                    self._send_json(400, {"error": "after_id and limit must be integers"})
                    return
                limit = max(1, min(limit, MAX_LIMIT))
                rows, more = fetch_after(db_path, kind, after_id, limit)
                last_id = rows[-1]["id"] if rows else after_id
                self._send_json(200, {"kind": kind, "rows": rows, "last_id": last_id, "more": more})
            else:
                self._send_json(404, {"error": "unknown path"})

        def do_POST(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path.rstrip("/")
            if not self._check_token():
                self._send_json(401, {"error": "invalid token"})
                return
            if path == "/api/run/speed":
                if run_speed_test_async(cfg, db_path):
                    self._send_json(202, {"status": "started"})
                else:
                    self._send_json(409, {"status": "busy"})
            else:
                self._send_json(404, {"error": "unknown path"})

    return Handler


def create_server(cfg: Config, db_path: str, started_at: str) -> http.server.ThreadingHTTPServer:
    handler = make_handler(cfg, db_path, started_at)
    httpd = http.server.ThreadingHTTPServer((cfg.bind, cfg.port), handler)
    httpd.daemon_threads = True
    return httpd
