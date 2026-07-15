"""JSON API for the frontend (charts) and health check."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from .. import VERSION
from ..aggregate import latency_series, pick_bucket, reach_series, speed_points, summary
from ..db import connect, get_network
from ..events import derive_events
from ..timerange import resolve_range

router = APIRouter(prefix="/api")

# a monitor counts as unreachable when sync hasn't succeeded for this many seconds
OFFLINE_AFTER = 180.0


def _open(request: Request):
    return connect(request.app.state.cfg.db_path)


def _net_id(conn, name: str) -> int:
    net = get_network(conn, name)
    if not net:
        raise HTTPException(404, f"Unknown network: {name}")
    return net["id"]


@router.get("/health")
def health():
    return {"status": "ok", "version": VERSION}


@router.get("/networks")
def networks(request: Request):
    """Networks + sync state + today's mini-summary for dashboard cards."""
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        t0, t1, _ = resolve_range("day", None, cfg.tz)
        out = []
        for net in conn.execute("SELECT * FROM networks ORDER BY name").fetchall():
            st = conn.execute("SELECT * FROM sync_status WHERE network_id=?",
                              (net["id"],)).fetchone()
            last_ok = st["last_ok_at"] if st else None
            s = summary(conn, net["id"], t0, t1, cfg.ping_interval)
            out.append({
                "name": net["name"],
                "label": net["label"],
                "sync": {
                    "last_ok_at": last_ok,
                    "last_error": st["last_error"] if st else None,
                    "consecutive_failures": st["consecutive_failures"] if st else 0,
                    "online": bool(last_ok and time.time() - last_ok < OFFLINE_AFTER),
                    "configured": any(m.name == net["name"] for m in cfg.monitors),
                },
                "today": s,
            })
        return out
    finally:
        conn.close()


@router.get("/net/{name}/summary")
def net_summary(request: Request, name: str, t0: float, t1: float):
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        return summary(conn, _net_id(conn, name), t0, t1, cfg.ping_interval)
    finally:
        conn.close()


@router.get("/net/{name}/series")
def net_series(request: Request, name: str, t0: float, t1: float):
    conn = _open(request)
    try:
        net_id = _net_id(conn, name)
        bucket = pick_bucket(t0, t1)
        return {
            "bucket": bucket,
            "latency": latency_series(conn, net_id, t0, t1, bucket),
            "reach": reach_series(conn, net_id, t0, t1, bucket),
            "speed": speed_points(conn, net_id, t0, t1),
        }
    finally:
        conn.close()


@router.get("/net/{name}/events")
def net_events(request: Request, name: str, t0: float, t1: float):
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        events = derive_events(conn, _net_id(conn, name), t0, t1, cfg.ping_interval)
        return [e.as_dict() for e in events]
    finally:
        conn.close()
