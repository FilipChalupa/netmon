"""HTML pages (Jinja2): dashboard, network detail, comparison."""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import connect, get_network
from ..timerange import custom_bounds, resolve_range

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _range_ctx(request: Request, range_: str, date_: str | None,
               from_: str | None = None, to_: str | None = None) -> dict:
    cfg = request.app.state.cfg
    today = datetime.date.today()

    if range_ == "custom":
        try:
            d0 = datetime.date.fromisoformat(from_ or "")
            d1 = datetime.date.fromisoformat(to_ or from_ or "")
        except ValueError:
            range_ = "day"  # malformed picker input → fall back to today
        else:
            if d1 < d0:
                d0, d1 = d1, d0
            t0, t1, label = custom_bounds(d0, d1, cfg.tz)
            span = datetime.timedelta(days=(d1 - d0).days + 1)
            day = datetime.timedelta(days=1)
            return {
                "range": "custom",
                "date": today.isoformat(),
                "t0": t0,
                "t1": t1,
                "range_label": label,
                "from_date": d0.isoformat(),
                "to_date": d1.isoformat(),
                "prev_from": (d0 - span).isoformat(),
                "prev_to": (d0 - day).isoformat(),
                "next_from": (d1 + day).isoformat() if d1 < today else None,
                "next_to": min(d1 + span, today).isoformat(),
                "is_today": d1 >= today,
            }

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
        "from_date": (d - datetime.timedelta(days=6)).isoformat() if range_ == "week"
                     else day,
        "to_date": day,
        "prev_date": (d - datetime.timedelta(days=1)).isoformat(),
        "next_date": (d + datetime.timedelta(days=1)).isoformat(),
        "is_today": d >= today,
    }


def _networks(conn):
    return conn.execute("SELECT name, label FROM networks ORDER BY name").fetchall()


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
