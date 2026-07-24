"""FastAPI application — evaluation server entry point.

Run:  uvicorn netmon_server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import VERSION
from .alerts import alert_loop
from .config import load_config
from .db import init_db
from .mcp_server import mcp
from .report import report_scheduler
from .routes import api, pages
from .sync import sync_forever

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("netmon.server")


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    app.state.cfg = cfg
    init_db(cfg.db_path)
    log.info("netmon server v%s — DB %s, %d monitors, sync every %.0f s",
             VERSION, cfg.db_path, len(cfg.monitors), cfg.sync_interval)

    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(sync_forever(cfg, stop), name="sync"),
        asyncio.create_task(report_scheduler(cfg, stop), name="report"),
        asyncio.create_task(alert_loop(cfg, stop), name="alerts"),
    ]
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="netmon", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")
# MCP endpoint for LLM clients: claude mcp add --transport http netmon http://host:8000/mcp
app.mount("/mcp", mcp.streamable_http_app())
app.include_router(api.router)
app.include_router(pages.router)


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    """HTML 404 for page URLs opened in a browser; API/MCP callers and all
    other status codes keep the standard JSON body."""
    if (exc.status_code == 404
            and not request.url.path.startswith(("/api/", "/mcp", "/static/"))
            and "text/html" in request.headers.get("accept", "")):
        return pages.not_found_page(request, exc.detail)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))
