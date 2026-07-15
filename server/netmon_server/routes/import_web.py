"""Web upload of historical logs — /import page.

Enabled only when NETMON_IMPORT_TOKEN is set; the upload form requires the
token. Accepts a .zip / .tar.gz / .tgz archive containing YYYYMMDD day
directories (possibly nested, e.g. log-mpc/20260616/*.csv) and imports them
into the chosen network. The import runs in a background thread; the page
polls its progress.
"""

from __future__ import annotations

import hmac
import os
import shutil
import tarfile
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from ..db import connect, get_or_create_network
from ..importer import find_log_roots, import_tree
from .pages import templates, _networks

router = APIRouter()

# in-memory job registry (single-process app); {id: {status, lines, network}}
_jobs: dict[str, dict] = {}


def _safe_extract(archive_path: str, dest: str) -> None:
    name = archive_path.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as zf:
            for info in zf.infolist():
                p = Path(info.filename)
                if p.is_absolute() or ".." in p.parts:
                    raise ValueError(f"Suspicious path in archive: {info.filename}")
            zf.extractall(dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(archive_path) as tf:
            tf.extractall(dest, filter="data")  # blocks absolute paths and '..'
    else:
        raise ValueError("Unsupported archive type — use .zip or .tar.gz")


def _run_job(job_id: str, db_path: str, workdir: str, archive: str,
             network: str, label: str) -> None:
    job = _jobs[job_id]
    log = job["lines"].append
    try:
        extract_dir = os.path.join(workdir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)
        log(f"Extracting {os.path.basename(archive)}…")
        _safe_extract(archive, extract_dir)

        roots = find_log_roots(extract_dir)
        if not roots:
            raise ValueError("No YYYYMMDD day directories found in the archive.")

        conn = connect(db_path)
        try:
            network_id = get_or_create_network(conn, network, label or None)
            total = {"days": 0, "files": 0, "skipped": 0, "rows": 0}
            for root in roots:
                log(f"Importing {os.path.relpath(root, extract_dir)}:")
                stats = import_tree(conn, network_id, root, force=False, log=log)
                for k in total:
                    total[k] += stats[k]
        finally:
            conn.close()
        log(f"Done: {total['days']} days, {total['files']} files, "
            f"{total['rows']} rows imported (+{total['skipped']} files skipped as already imported).")
        job["status"] = "done"
    except Exception as e:
        log(f"ERROR: {e}")
        job["status"] = "error"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


@router.get("/import", response_class=HTMLResponse)
def import_page(request: Request, job: str | None = None):
    cfg = request.app.state.cfg
    conn = connect(cfg.db_path)
    try:
        nets = [dict(n) for n in _networks(conn)]
    finally:
        conn.close()
    return templates.TemplateResponse(request, "import.html", {
        "networks": nets,
        "enabled": bool(cfg.import_token),
        "job": _jobs.get(job),
        "job_id": job if job in _jobs else None,
    })


@router.post("/import")
async def import_upload(request: Request,
                        token: str = Form(""),
                        network: str = Form(...),
                        label: str = Form(""),
                        file: UploadFile = File(...)):
    cfg = request.app.state.cfg
    if not cfg.import_token:
        raise HTTPException(403, "Import is disabled — set NETMON_IMPORT_TOKEN.")
    if not hmac.compare_digest(token.strip(), cfg.import_token):
        raise HTTPException(403, "Invalid import token.")
    network = network.strip()
    if not network:
        raise HTTPException(400, "Network name is required.")

    # spool the upload next to the database (same filesystem, survives big files)
    workdir = tempfile.mkdtemp(prefix="import-",
                               dir=os.path.dirname(cfg.db_path) or ".")
    archive = os.path.join(workdir, os.path.basename(file.filename or "upload.zip"))
    with open(archive, "wb") as f:
        shutil.copyfileobj(file.file, f)

    job_id = uuid.uuid4().hex[:12]
    _jobs[job_id] = {"status": "running", "network": network,
                     "lines": [f"Received {os.path.basename(archive)} "
                               f"({os.path.getsize(archive) // 1024} kB) → network '{network}'"]}
    threading.Thread(target=_run_job,
                     args=(job_id, cfg.db_path, workdir, archive, network, label.strip()),
                     name=f"import-{job_id}", daemon=True).start()
    return RedirectResponse(f"/import?job={job_id}", status_code=303)
