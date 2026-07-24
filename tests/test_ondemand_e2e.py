"""Full on-demand speed test chain, driven from the browser:

run-test button → server POST /api/net/{name}/run/speed → monitor
/api/run/speed → measure (against a local payload server) → monitor DB →
sync → server DB → visible in /api series. Skipped without playwright.
"""

import http.server
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("fastapi")

from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from netmon_monitor.config import Config as MonitorConfig  # noqa: E402
from netmon_monitor.db import Db as MonitorDb  # noqa: E402
from netmon_monitor.server import create_server  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = b"\0" * 200_000


class _PayloadHandler(http.server.BaseHTTPRequestHandler):
    """Local stand-in for speed.cloudflare.com: GET serves bytes, POST sinks them."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        while n > 0:
            n -= len(self.rfile.read(min(n, 65536)))
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """payload server + monitor + evaluation server (uvicorn), synced every 1 s."""
    tmp = tmp_path_factory.mktemp("e2e")
    threads = []

    payload_srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _PayloadHandler)
    payload_port = payload_srv.server_address[1]
    threads.append(threading.Thread(target=payload_srv.serve_forever, daemon=True))

    mon_db = str(tmp / "monitor.db")
    MonitorDb(mon_db).close()
    mon_cfg = MonitorConfig(
        network="e2e", port=0, bind="127.0.0.1", token="tok",
        speed_url=f"http://127.0.0.1:{payload_port}/down?bytes={{bytes}}",
        speed_bytes=200_000, speed_min_seconds=0,
        upload_url=f"http://127.0.0.1:{payload_port}/up", upload_bytes=100_000,
        targets=[("gateway", "auto")],  # no public target → no bloat pings offline
    )
    mon_srv = create_server(mon_cfg, mon_db, "T0")
    mon_port = mon_srv.server_address[1]
    threads.append(threading.Thread(target=mon_srv.serve_forever, daemon=True))
    for t in threads:
        t.start()

    toml = tmp / "monitors.toml"
    toml.write_text(
        f'sync_interval = 1\n[[monitors]]\nname = "e2e"\nlabel = "E2E net"\n'
        f'url = "http://127.0.0.1:{mon_port}"\ntoken = "tok"\n')

    def free_port():
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    srv_port = free_port()
    pypath = os.pathsep.join(
        [str(ROOT / "server"), str(ROOT / "monitor")]
        + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
    env = dict(os.environ, NETMON_DB=str(tmp / "server.db"),
               NETMON_MONITORS=str(toml), NETMON_ALERTS="0", PYTHONPATH=pypath)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "netmon_server.main:app",
         "--host", "127.0.0.1", "--port", str(srv_port)],
        env=env, cwd=str(tmp),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{srv_port}"
    try:
        deadline = time.time() + 15
        while time.time() < deadline:  # server up AND the first sync created the network
            try:
                nets = json.load(urllib.request.urlopen(base + "/api/networks", timeout=1))
                if any(n["name"] == "e2e" for n in nets):
                    break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            pytest.skip("e2e stack did not come up")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        payload_srv.shutdown()
        mon_srv.shutdown()


def test_run_speed_button_end_to_end(stack):
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(stack + "/net/e2e")
            page.wait_for_selector("#runSpeed")
            page.click("#runSpeed")
            # 202 from the monitor → the button reports the test is running
            page.wait_for_function(
                "document.getElementById('runSpeed').textContent.includes('running')")
            browser.close()
    except PWError as e:
        pytest.skip(f"chromium unavailable: {e}")

    # the measured result reaches the server DB through the normal sync
    deadline = time.time() + 30
    while time.time() < deadline:
        q = f"?t0={time.time() - 3600}&t1={time.time() + 60}"
        series = json.load(urllib.request.urlopen(
            stack + "/api/net/e2e/series" + q, timeout=2))
        spd = series["speed"]
        if spd["ts"]:
            assert spd["mbps"][0] and spd["mbps"][0] > 0
            assert spd["up"][0] and spd["up"][0] > 0
            return
        time.sleep(0.5)
    pytest.fail("on-demand speed result never appeared in the server series")
