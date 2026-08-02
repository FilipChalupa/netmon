"""JSON API for the frontend (charts) and health check."""

from __future__ import annotations

import csv
import datetime
import io
import os
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .. import VERSION
from ..aggregate import (attach_diags, daily_heatmap, latency_series, pick_bucket,
                         reach_series, speed_points, summary)
from ..db import KINDS, connect, get_network, set_network_description
from ..events import derive_events, derive_reach_events, merge_events
from ..notes import create_note, delete_note, list_notes, update_note
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
            s = summary(conn, net["id"], t0, t1, cfg.ping_interval,
                        cfg.alert_reach_fails)
            mon = next((m for m in cfg.monitors if m.name == net["name"]), None)
            out.append({
                "name": net["name"],
                "label": net["label"],
                "description": net["description"],
                "monitor_version": net["monitor_version"],
                "plan": {"down": mon.plan_down or None, "up": mon.plan_up or None}
                        if mon and (mon.plan_down or mon.plan_up) else None,
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


@router.get("/db/stats")
def db_stats(request: Request):
    """DB size + per-kind row counts — shown on the help page so growth
    doesn't come as a surprise."""
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        kinds = {}
        for kind in KINDS:
            row = conn.execute(f"SELECT COUNT(*) AS n, MIN(ts_epoch) AS oldest "
                               f"FROM {kind}").fetchone()
            kinds[kind] = {"rows": row["n"], "oldest": row["oldest"]}
    finally:
        conn.close()
    size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            size += os.path.getsize(cfg.db_path + suffix)
        except OSError:
            pass
    return {"size_bytes": size, "kinds": kinds,
            "retention_days": cfg.retention_days}


@router.get("/monitors/health")
def monitors_health(request: Request):
    """Light sync status per network — feeds the global unhealthy-monitor
    banner on every page, so it skips the expensive daily summaries."""
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        out = []
        for net in conn.execute("SELECT id, name, label FROM networks "
                                "ORDER BY name").fetchall():
            st = conn.execute("SELECT * FROM sync_status WHERE network_id=?",
                              (net["id"],)).fetchone()
            last_ok = st["last_ok_at"] if st else None
            out.append({
                "name": net["name"],
                "label": net["label"],
                "configured": any(m.name == net["name"] for m in cfg.monitors),
                "online": bool(last_ok and time.time() - last_ok < OFFLINE_AFTER),
                "last_ok_at": last_ok,
                "last_error": st["last_error"] if st else None,
                "consecutive_failures": st["consecutive_failures"] if st else 0,
            })
        return out
    finally:
        conn.close()


@router.get("/net/{name}/summary")
def net_summary(request: Request, name: str, t0: float, t1: float):
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        return summary(conn, _net_id(conn, name), t0, t1, cfg.ping_interval,
                       cfg.alert_reach_fails)
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


@router.post("/net/{name}/run/speed")
async def run_speed(request: Request, name: str):
    """Trigger an immediate speed test on the network's monitor. The result
    arrives through the normal sync within a minute or two."""
    cfg = request.app.state.cfg
    mon = next((m for m in cfg.monitors if m.name == name), None)
    if mon is None:
        raise HTTPException(404, f"No configured monitor for network: {name}")
    headers = {"X-Netmon-Token": mon.token} if mon.token else {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{mon.url}/api/run/speed", headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Monitor unreachable: {e}")
    return JSONResponse(resp.json(), status_code=resp.status_code)


@router.get("/net/{name}/heatmap")
def net_heatmap(request: Request, name: str, days: int = 365):
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        return {"days": daily_heatmap(conn, _net_id(conn, name), cfg.tz,
                                      min(max(days, 1), 2 * 366))}
    finally:
        conn.close()


# raw-data exports: kind → (SQL over [network_id, t0, t1], CSV columns)
EXPORT_KINDS = {
    "latency": ("SELECT ts_iso, ts_epoch, target, status, rtt_ms FROM latency "
                "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
                ["ts_iso", "ts_epoch", "target", "status", "rtt_ms"]),
    "reach": ("SELECT ts_iso, ts_epoch, dns_ms, tcp_ms, tls_ms, http_code, status "
              "FROM reach WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? "
              "ORDER BY ts_epoch",
              ["ts_iso", "ts_epoch", "dns_ms", "tcp_ms", "tls_ms", "http_code", "status"]),
    "speed": ("SELECT ts_iso, ts_epoch, down_mbps, up_mbps, bytes, seconds, "
              "http_code, idle_rtt_ms, loaded_rtt_ms FROM speed "
              "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
              ["ts_iso", "ts_epoch", "down_mbps", "up_mbps", "bytes", "seconds",
               "http_code", "idle_rtt_ms", "loaded_rtt_ms"]),
}


@router.get("/net/{name}/export.csv")
def net_export(request: Request, name: str, kind: str, t0: float, t1: float):
    """The visible range as CSV — raw rows for latency/reach/speed, derived
    events for kind=events. Streams, so a year of pings won't buffer in RAM."""
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        net_id = _net_id(conn, name)
        if kind == "events":
            events = merge_events(
                derive_events(conn, net_id, t0, t1, cfg.ping_interval),
                derive_reach_events(conn, net_id, t0, t1, cfg.alert_reach_fails))
    finally:
        conn.close()
    if kind != "events" and kind not in EXPORT_KINDS:
        raise HTTPException(400, f"Unknown export kind: {kind}")

    def fmt_day(t: float) -> str:
        return datetime.datetime.fromtimestamp(t).strftime("%Y%m%d")

    filename = f"netmon-{name}-{kind}-{fmt_day(t0)}-{fmt_day(t1)}.csv"

    def stream():
        buf = io.StringIO()
        w = csv.writer(buf)
        if kind == "events":
            w.writerow(["start_iso", "end_iso", "duration_s", "scope", "note"])
            for e in events:
                d = e.as_dict()
                w.writerow([d["start"], d["end"], d["dur"], d["scope"], d["note"]])
            yield buf.getvalue()
            return
        sql, cols = EXPORT_KINDS[kind]
        w.writerow(cols)
        c = connect(cfg.db_path)  # own connection — outlives the request handler
        try:
            for i, row in enumerate(c.execute(sql, (net_id, t0, t1))):
                w.writerow([row[col] for col in cols])
                if i % 2000 == 1999:  # flush in chunks, not per row
                    yield buf.getvalue()
                    buf.seek(0)
                    buf.truncate(0)
            yield buf.getvalue()
        finally:
            c.close()

    return StreamingResponse(stream(), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="{filename}"'})


class DescriptionIn(BaseModel):
    text: str = ""


@router.put("/net/{name}/description")
def net_description(request: Request, name: str, body: DescriptionIn):
    """Free-text note about the network itself (tariff, price, FUP, ISP
    contact…) shown on its detail page. Empty text clears it."""
    text = body.text.strip()[:500]
    conn = _open(request)
    try:
        if not set_network_description(conn, name, text):
            raise HTTPException(404, f"Unknown network: {name}")
        return {"ok": True, "description": text or None}
    finally:
        conn.close()


class NoteIn(BaseModel):
    text: str
    ts_epoch: float
    networks: list[str] = []


@router.get("/notes")
def notes_list(request: Request, t0: float, t1: float, nets: str | None = None):
    """Notes in range; nets is a comma-separated filter (general notes always match)."""
    names = [n.strip() for n in nets.split(",") if n.strip()] if nets else None
    conn = _open(request)
    try:
        return list_notes(conn, t0, t1, names)
    finally:
        conn.close()


@router.post("/notes", status_code=201)
def notes_create(request: Request, note: NoteIn):
    conn = _open(request)
    try:
        return create_note(conn, note.ts_epoch, note.text, note.networks)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


class NotePatch(BaseModel):
    text: str | None = None
    ts_epoch: float | None = None
    networks: list[str] | None = None


@router.patch("/notes/{note_id}")
def notes_update(request: Request, note_id: int, patch: NotePatch):
    conn = _open(request)
    try:
        note = update_note(conn, note_id, patch.text, patch.ts_epoch, patch.networks)
        if note is None:
            raise HTTPException(404, f"Unknown note: {note_id}")
        return note
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.delete("/notes/{note_id}")
def notes_delete(request: Request, note_id: int):
    conn = _open(request)
    try:
        if not delete_note(conn, note_id):
            raise HTTPException(404, f"Unknown note: {note_id}")
        return {"ok": True}
    finally:
        conn.close()


@router.get("/net/{name}/events")
def net_events(request: Request, name: str, t0: float, t1: float):
    cfg = request.app.state.cfg
    conn = _open(request)
    try:
        net_id = _net_id(conn, name)
        events = merge_events(
            derive_events(conn, net_id, t0, t1, cfg.ping_interval),
            derive_reach_events(conn, net_id, t0, t1, cfg.alert_reach_fails))
        return attach_diags(conn, net_id, [e.as_dict() for e in events], t0, t1)
    finally:
        conn.close()
