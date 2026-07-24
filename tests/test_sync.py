"""Sync: incremental pull from a monitor, cursors, token."""

import asyncio
import threading

import httpx
import pytest

from netmon_monitor.config import Config as MonitorConfig
from netmon_monitor.db import Db as MonitorDb
from netmon_monitor.server import create_server

from netmon_server.config import MonitorCfg
from netmon_server.db import connect, init_db
from netmon_server.sync import pull_monitor, sync_once
from netmon_server.config import ServerConfig


@pytest.fixture
def monitor(tmp_path):
    """Monitor DB with seeded data + a running HTTP API on an ephemeral port."""
    mdb = MonitorDb(str(tmp_path / "monitor.db"))
    for i in range(7):
        ts = 1000.0 + 2 * i
        iso = f"2025-07-01T10:00:{2 * i:02d}+02:00"
        mdb.insert_latency(ts, iso, "gateway", "10.0.0.1", "ok", 0.8)
        mdb.insert_latency(ts, iso, "google", "8.8.8.8", "LOSS", None)
    mdb.insert_uptime(1000.0, "2025-07-01T10:00:00+02:00", "START")
    mdb.insert_reach(1000.0, "2025-07-01T10:00:00+02:00", 4.0, 2.0, 30.0, 204, "ok")
    mdb.insert_speed(1000.0, "2025-07-01T10:00:00+02:00", 100.5, 1000000, 0.08, 200,
                     42.5, 8.0, 31.0)

    cfg = MonitorConfig(network="testnet", port=0, bind="127.0.0.1", token="tok")
    httpd = create_server(cfg, str(tmp_path / "monitor.db"), "2025-07-01T10:00:00+02:00")
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    mdb.close()


@pytest.fixture
def server_conn(tmp_path):
    db_path = str(tmp_path / "server.db")
    init_db(db_path)
    c = connect(db_path)
    yield c
    c.close()


def _pull(conn, url, token="tok", page_limit=None):
    async def run():
        import netmon_server.sync as sync_mod
        old = sync_mod.PAGE_LIMIT
        if page_limit:
            sync_mod.PAGE_LIMIT = page_limit
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                return await pull_monitor(conn, client,
                                          MonitorCfg(name="testnet", url=url, token=token))
        finally:
            sync_mod.PAGE_LIMIT = old
    return asyncio.run(run())


def test_pull_and_cursor(server_conn, monitor):
    # paginated (limit 5), all 14 + 1 + 1 + 1 rows get pulled
    n = _pull(server_conn, monitor, page_limit=5)
    assert n == 17

    rows = server_conn.execute(
        "SELECT src_id, target, status FROM latency ORDER BY src_id").fetchall()
    assert len(rows) == 14
    assert rows[0]["src_id"] == 1 and rows[-1]["src_id"] == 14

    spd = server_conn.execute(
        "SELECT down_mbps, up_mbps, idle_rtt_ms, loaded_rtt_ms FROM speed").fetchone()
    assert tuple(spd) == (100.5, 42.5, 8.0, 31.0)

    cur = server_conn.execute(
        "SELECT kind, last_src_id FROM sync_cursor ORDER BY kind").fetchall()
    assert {r["kind"]: r["last_src_id"] for r in cur} == \
        {"latency": 14, "reach": 1, "speed": 1, "uptime": 1}

    # second pull: nothing new
    assert _pull(server_conn, monitor) == 0


def test_wrong_token_raises_and_is_tracked(server_conn, monitor):
    with pytest.raises(httpx.HTTPStatusError):
        _pull(server_conn, monitor, token="spatny")

    # sync_once catches the error and records it in sync_status
    cfg = ServerConfig(monitors=[MonitorCfg(name="testnet", url=monitor, token="spatny")])

    async def run():
        async with httpx.AsyncClient(timeout=5.0) as client:
            await sync_once(server_conn, client, cfg)
    asyncio.run(run())

    st = server_conn.execute("SELECT * FROM sync_status").fetchone()
    assert st["consecutive_failures"] == 1
    assert "401" in st["last_error"]
