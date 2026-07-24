"""HTML pages (Jinja2): dashboard, network detail, comparison."""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import connect, get_network
from ..timerange import custom_ctx, resolve_range

from .. import VERSION

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
templates.env.globals["VERSION"] = VERSION


def _range_ctx(request: Request, range_: str, date_: str | None,
               from_: str | None = None, to_: str | None = None) -> dict:
    cfg = request.app.state.cfg
    today = datetime.date.today()

    if range_ == "custom":
        try:
            ctx = custom_ctx(from_, to_, cfg.tz)
        except ValueError:
            range_ = "day"  # malformed picker input → fall back to today
        else:
            ctx["date"] = today.isoformat()
            return ctx

    t0, t1, label = resolve_range(range_, date_, cfg.tz)
    day = date_ or today.isoformat()
    d = datetime.date.fromisoformat(day)
    return {
        "range": range_,
        "date": day,
        "t0": t0,
        "t1": t1,
        "range_label": label,
        # picker prefill mirroring the active range (all → today)
        "from_date": (d - datetime.timedelta(
            days={"week": 6, "24h": 1, "48h": 2}.get(range_, 0))).isoformat(),
        "to_date": day,
        "prev_date": (d - datetime.timedelta(days=1)).isoformat(),
        "next_date": (d + datetime.timedelta(days=1)).isoformat(),
        "is_today": d >= today,
    }


def _networks(conn):
    return conn.execute("SELECT name, label FROM networks ORDER BY name").fetchall()


def not_found_page(request: Request, detail: str | None = None) -> HTMLResponse:
    """Friendly HTML 404 for page URLs (the JSON handler stays for /api).

    Best-effort nav: a DB hiccup must not turn a 404 into a 500."""
    try:
        conn = connect(request.app.state.cfg.db_path)
        try:
            nets = [dict(n) for n in _networks(conn)]
        finally:
            conn.close()
    except Exception:
        nets = []
    message = detail if detail and detail != "Not Found" else \
        f"The address {request.url.path} doesn't exist — maybe an old link, or a typo."
    return templates.TemplateResponse(
        request, "404.html", {"networks": nets, "message": message},
        status_code=404)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = connect(request.app.state.cfg.db_path)
    try:
        nets = [dict(n) for n in _networks(conn)]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "dashboard.html", {"networks": nets})


@router.get("/net/{name}", response_class=HTMLResponse)
def network_detail(request: Request, name: str, range: str = "day", date: str | None = None,
                   from_: str | None = Query(None, alias="from"), to: str | None = None):
    conn = connect(request.app.state.cfg.db_path)
    try:
        net = get_network(conn, name)
        if not net:
            raise HTTPException(404, f"Unknown network: {name}")
        nets = _networks(conn)
    finally:
        conn.close()
    ctx = {"net": dict(net), "networks": [dict(n) for n in nets], "extra_qs": ""}
    ctx.update(_range_ctx(request, range, date, from_, to))
    return templates.TemplateResponse(request, "network.html", ctx)


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    """Served from the root so the worker's scope covers the whole app."""
    return FileResponse(
        Path(__file__).resolve().parent.parent / "static" / "sw.js",
        media_type="application/javascript")


@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    """Read-only shareable status — big OK/OUTAGE indicator, no controls."""
    return templates.TemplateResponse(request, "status.html", {})


@router.get("/help", response_class=HTMLResponse)
def help_page(request: Request):
    conn = connect(request.app.state.cfg.db_path)
    try:
        nets = [dict(n) for n in _networks(conn)]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "help.html", {"networks": nets})


@router.get("/compare", response_class=HTMLResponse)
def compare(request: Request, range: str = "day", date: str | None = None,
            nets: str | None = None,
            from_: str | None = Query(None, alias="from"), to: str | None = None):
    conn = connect(request.app.state.cfg.db_path)
    try:
        all_nets = [dict(n) for n in _networks(conn)]
    finally:
        conn.close()
    selected = [n.strip() for n in nets.split(",") if n.strip()] if nets \
        else [n["name"] for n in all_nets]
    ctx = {"networks": all_nets, "selected": selected,
           "nets_param": nets or "",
           "extra_qs": f"&nets={nets}" if nets else ""}
    ctx.update(_range_ctx(request, range, date, from_, to))
    return templates.TemplateResponse(request, "compare.html", ctx)
