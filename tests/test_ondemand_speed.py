"""On-demand speed test: monitor endpoint runs a test and stores the result."""

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from netmon_monitor import server as mon_server
from netmon_monitor.config import Config
from netmon_monitor.db import Db


@pytest.fixture
def monitor(tmp_path):
    db_path = str(tmp_path / "monitor.db")
    Db(db_path).close()   # create the schema
    cfg = Config(port=0, bind="127.0.0.1")
    httpd = mon_server.create_server(cfg, db_path, "T0")
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", db_path, cfg
    httpd.shutdown()
    t.join(timeout=5)


def _post(url):
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_run_speed_stores_result(monitor, monkeypatch):
    base, db_path, _ = monitor
    monkeypatch.setattr(mon_server, "measure_speed",
                        lambda cfg, stop: (123.4, 1_000_000, 1.5, 200, 45.6, 8.0, 31.0))
    status, body = _post(base + "/api/run/speed")
    assert (status, body["status"]) == (202, "started")

    deadline = time.time() + 5
    row = None
    while time.time() < deadline and row is None:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT down_mbps, up_mbps, idle_rtt_ms, loaded_rtt_ms FROM speed").fetchone()
        conn.close()
        time.sleep(0.05)
    assert row is not None and row == (123.4, 45.6, 8.0, 31.0)


def test_second_request_while_running_is_busy(monitor, monkeypatch):
    base, _, _ = monitor
    release = threading.Event()
    monkeypatch.setattr(mon_server, "measure_speed",
                        lambda cfg, stop: (release.wait(5), None, None, None, 0, None, None, None)[1:])
    try:
        assert _post(base + "/api/run/speed")[0] == 202
        status, body = _post(base + "/api/run/speed")
        assert (status, body["status"]) == (409, "busy")
    finally:
        release.set()
        time.sleep(0.2)   # let the worker release the lock for other tests


def test_unknown_post_path_is_404(monitor):
    base, _, _ = monitor
    assert _post(base + "/api/run/bogus")[0] == 404
