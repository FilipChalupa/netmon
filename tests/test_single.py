"""Single mode: config override plumbing and server-config construction."""

import pytest

pytest.importorskip("fastapi")

from netmon_monitor.config import Config as MonitorConfig  # noqa: E402
from netmon_server.config import (ServerConfig, load_config,  # noqa: E402
                                  set_config_override)
from netmon_server.single import build_server_config  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_override():
    yield
    set_config_override(None)


def test_config_override_wins_over_env(monkeypatch):
    monkeypatch.setenv("NETMON_DB", "/env/ignored.db")
    override = ServerConfig(db_path="/tmp/x.db")
    set_config_override(override)
    assert load_config() is override
    set_config_override(None)
    assert load_config().db_path == "/env/ignored.db"


def test_build_server_config_points_at_local_monitor():
    mcfg = MonitorConfig(network="loft", port=9999, token="s3cret",
                         ping_interval=1.0)
    scfg = build_server_config(mcfg, "/data", "Europe/Prague")
    assert scfg.db_path == "/data/server.db"
    assert scfg.ping_interval == 1.0
    [mon] = scfg.monitors
    assert (mon.name, mon.url, mon.token) == ("loft", "http://127.0.0.1:9999", "s3cret")
