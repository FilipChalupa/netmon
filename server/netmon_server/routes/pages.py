"""HTML pages (Jinja2): dashboard, network detail, comparison."""

from __future__ import annotations

import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from ..db import connect, get_network
from ..timerange import resolve_range

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


def _range_ctx(request: Request, range_: str, date_: str | None) -> dict:
    cfg = request.app.state.cfg
    t0, t1, label = resolve_range(range_, date_, cfg.tz)
    day = date_ or datetime.date.today().isoformat()
    d = datetime.date.fromisoformat(day)
    return {
        "range": range_,
        "date": day,
        "t0": t0,
        "t1": t1,
        "range_label": label,
        "prev_date": (d - datetime.timedelta(days=1)).isoformat(),
        "next_date": (d + datetime.timedelta(days=1)).isoformat(),
        "is_today": d >= datetime.date.today(),
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
def network_detail(request: Request, name: str, range: str = "day", date: str | None = None):
    conn = connect(request.app.state.cfg.db_path)
    try:
        net = get_network(conn, name)
        if not net:
            raise HTTPException(404, f"Unknown network: {name}")
        nets = _networks(conn)
    finally:
        conn.close()
    ctx = {"net": dict(net), "networks": [dict(n) for n in nets]}
    ctx.update(_range_ctx(request, range, date))
    return templates.TemplateResponse(request, "network.html", ctx)


@router.get("/compare", response_class=HTMLResponse)
def compare(request: Request, range: str = "day", date: str | None = None,
            nets: str | None = None):
    conn = connect(request.app.state.cfg.db_path)
    try:
        all_nets = [dict(n) for n in _networks(conn)]
    finally:
        conn.close()
    selected = [n.strip() for n in nets.split(",") if n.strip()] if nets \
        else [n["name"] for n in all_nets]
    ctx = {"networks": all_nets, "selected": selected}
    ctx.update(_range_ctx(request, range, date))
    return templates.TemplateResponse(request, "compare.html", ctx)
