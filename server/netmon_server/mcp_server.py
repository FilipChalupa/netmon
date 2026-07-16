"""MCP server — lets LLM clients (Claude Code, Claude Desktop) query netmon.

Mounted at /mcp on the main FastAPI app (streamable HTTP transport), so it
shares the port, the database and the trust boundary of the web UI (no extra
auth — run it on a trusted network such as the tailnet).

Tools are thin wrappers over the same functions the JSON API uses. Everything
is read-only except add_note; destructive operations stay in the web UI.
"""

from __future__ import annotations

import datetime
import time
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP

from .aggregate import daily_heatmap, speed_points, summary
from .config import load_config
from .db import connect
from .notes import create_note
from .notes import list_notes as _list_notes
from .timerange import custom_ctx, resolve_range

mcp = FastMCP(
    "netmon",
    instructions=(
        "Network quality monitoring (ping latency/loss, DNS/TCP/TLS "
        "reachability, download speed, outages, public IP, user notes) for "
        "one or more networks. Times are ISO strings in the server timezone; "
        "ranges accept day/week/24h/48h/all with an optional date, or "
        "start/end (YYYY-MM-DD or YYYY-MM-DDTHH:MM)."
    ),
    stateless_http=True,
    streamable_http_path="/",
)


def _conn():
    return connect(load_config().db_path)


def _net_id(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM networks WHERE name=?", (name,)).fetchone()
    if not row:
        known = [r["name"] for r in
                 conn.execute("SELECT name FROM networks ORDER BY name")]
        raise ValueError(f"Unknown network {name!r}; known: {', '.join(known) or '(none)'}")
    return row["id"]


def _bounds(range_: str, date: str | None, start: str | None,
            end: str | None) -> tuple[float, float, str]:
    cfg = load_config()
    if start:
        ctx = custom_ctx(start, end, cfg.tz)
        return ctx["t0"], ctx["t1"], ctx["range_label"]
    return resolve_range(range_, date, cfg.tz)


@mcp.tool()
def list_networks() -> list[dict]:
    """All monitored networks with their labels and monitor sync state."""
    conn = _conn()
    try:
        out = []
        for net in conn.execute("SELECT * FROM networks ORDER BY name"):
            st = conn.execute("SELECT * FROM sync_status WHERE network_id=?",
                              (net["id"],)).fetchone()
            last_ok = st["last_ok_at"] if st else None
            out.append({
                "name": net["name"],
                "label": net["label"],
                "monitor_online": bool(last_ok and time.time() - last_ok < 180),
                "last_sync_iso": (datetime.datetime.fromtimestamp(last_ok).isoformat(
                    timespec="seconds") if last_ok else None),
            })
        return out
    finally:
        conn.close()


@mcp.tool()
def get_summary(network: str, range: str = "day", date: str | None = None,
                start: str | None = None, end: str | None = None) -> dict:
    """Full quality summary for a network and time range: per-target latency
    and packet loss, download speed stats, measurement coverage, outage
    events, public IP (with changes) and the events summary.

    Use range=day|week|24h|48h|all (+ optional date=YYYY-MM-DD for day/week),
    or an explicit start/end (YYYY-MM-DD or YYYY-MM-DDTHH:MM)."""
    cfg = load_config()
    t0, t1, label = _bounds(range, date, start, end)
    conn = _conn()
    try:
        s = summary(conn, _net_id(conn, network), t0, t1, cfg.ping_interval)
    finally:
        conn.close()
    s["period_label"] = label
    return s


@mcp.tool()
def get_speed_history(network: str, range: str = "week", date: str | None = None,
                      start: str | None = None, end: str | None = None) -> dict:
    """Individual download speed tests (hourly points) in the range:
    parallel arrays ts (epoch seconds) and mbps."""
    t0, t1, label = _bounds(range, date, start, end)
    conn = _conn()
    try:
        pts = speed_points(conn, _net_id(conn, network), t0, t1)
    finally:
        conn.close()
    pts["period_label"] = label
    return pts


@mcp.tool()
def get_daily_heatmap(network: str, days: int = 90) -> list[dict]:
    """Per-day internet packet loss (public targets) for the last N days —
    the long-term view; loss is a percentage, null = monitor not measuring."""
    cfg = load_config()
    conn = _conn()
    try:
        return daily_heatmap(conn, _net_id(conn, network), cfg.tz,
                             min(max(days, 1), 732))
    finally:
        conn.close()


@mcp.tool()
def get_notes(range: str = "week", date: str | None = None,
              start: str | None = None, end: str | None = None,
              network: str | None = None) -> list[dict]:
    """User notes in the range; with network set, only general notes and
    notes scoped to it."""
    t0, t1, _ = _bounds(range, date, start, end)
    conn = _conn()
    try:
        return _list_notes(conn, t0, t1, [network] if network else None)
    finally:
        conn.close()


@mcp.tool()
def add_note(text: str, at: str | None = None,
             networks: list[str] | None = None) -> dict:
    """Add a note pinned to a moment (shown in charts and daily reports).

    at: ISO date/datetime in the server timezone, default now.
    networks: monitor names the note applies to; empty/omitted = general
    note that applies to all networks."""
    cfg = load_config()
    if at:
        dt = datetime.datetime.fromisoformat(at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(cfg.tz))
        ts = dt.timestamp()
    else:
        ts = time.time()
    conn = _conn()
    try:
        return create_note(conn, ts, text, networks or [])
    finally:
        conn.close()
