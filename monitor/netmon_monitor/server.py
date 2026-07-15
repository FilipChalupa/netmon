"""Mini HTTP API for the evaluation server (pull over Tailscale).

Endpoints:
  GET /api/health                          → {status, time, started_at}
  GET /api/info                            → {network, version, hostname, targets, intervals}
  GET /api/data/{kind}?after_id=N&limit=M  → {kind, rows, last_id, more}

Authentication: X-Netmon-Token header (only when a token is configured).
"""

from __future__ import annotations

import hmac
import http.server
import json
import socket
import urllib.parse

from . import VERSION
from .config import Config
from .db import KIND_COLUMNS, fetch_after
from .workers import now_iso

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

    return Handler


def create_server(cfg: Config, db_path: str, started_at: str) -> http.server.ThreadingHTTPServer:
    handler = make_handler(cfg, db_path, started_at)
    httpd = http.server.ThreadingHTTPServer((cfg.bind, cfg.port), handler)
    httpd.daemon_threads = True
    return httpd
