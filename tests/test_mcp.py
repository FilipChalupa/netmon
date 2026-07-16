"""MCP tools — direct calls to the tool functions over a temp DB."""

import pytest

pytest.importorskip("mcp")

from netmon_server import mcp_server  # noqa: E402
from netmon_server.db import connect, get_or_create_network, init_db  # noqa: E402
from netmon_server.notes import list_notes  # noqa: E402


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "server.db")
    init_db(path)
    monkeypatch.setenv("NETMON_DB", path)
    monkeypatch.setenv("NETMON_MONITORS", str(tmp_path / "none.toml"))
    conn = connect(path)
    get_or_create_network(conn, "home", "Home")
    conn.close()
    return path


def test_list_networks(db):
    nets = mcp_server.list_networks()
    assert [n["name"] for n in nets] == ["home"]
    assert nets[0]["monitor_online"] is False


def test_get_summary_and_unknown_network(db):
    s = mcp_server.get_summary("home", range="day")
    assert "period_label" in s and "targets" in s and "pubip" in s
    with pytest.raises(ValueError, match="known: home"):
        mcp_server.get_summary("nope")


def test_add_note_roundtrip(db):
    note = mcp_server.add_note("MCP note", at="2026-07-10T14:00", networks=["home"])
    assert note["text"] == "MCP note"
    got = mcp_server.get_notes(start="2026-07-10", end="2026-07-10")
    assert [n["id"] for n in got] == [note["id"]]
    conn = connect(db)
    try:
        assert len(list_notes(conn, 0, 2e12)) == 1
    finally:
        conn.close()


def test_add_note_default_now(db):
    import time
    note = mcp_server.add_note("now note")
    assert abs(note["ts_epoch"] - time.time()) < 5
    assert note["networks"] == []
