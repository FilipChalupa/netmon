"""FastAPI application — evaluation server entry point.

Run:  uvicorn netmon_server.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import VERSION
from .alerts import alert_loop
from .config import load_config
from .db import init_db
from .report import report_scheduler
from .routes import api, import_web, pages
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
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="netmon", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")),
          name="static")
app.include_router(api.router)
app.include_router(pages.router)
app.include_router(import_web.router)
