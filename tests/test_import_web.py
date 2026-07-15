"""Web import: token gate, zip upload, background job, idempotency."""

import io
import time
import zipfile
from pathlib import Path

import pytest

FIXTURE_LOG = Path(__file__).parent / "fixtures" / "log"


def _fixture_zip(prefix: str = "log-mpc") -> bytes:
    """Zip the fixture log tree the way Windows would (nested folder)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for p in FIXTURE_LOG.rglob("*"):
            if p.is_file():
                zf.write(p, f"{prefix}/{p.relative_to(FIXTURE_LOG)}")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NETMON_DB", str(tmp_path / "netmon.db"))
    monkeypatch.setenv("NETMON_MONITORS", str(tmp_path / "missing.toml"))
    monkeypatch.setenv("NETMON_IMPORT_TOKEN", "tajemstvi")
    monkeypatch.setenv("NETMON_ALERTS", "0")
    from fastapi.testclient import TestClient
    from netmon_server.main import app
    with TestClient(app) as c:
        yield c


def _wait_for_job(client, redirect_url, timeout=15.0):
    from netmon_server.routes.import_web import _jobs
    job_id = redirect_url.split("job=")[1]
    deadline = time.time() + timeout
    while _jobs[job_id]["status"] == "running" and time.time() < deadline:
        time.sleep(0.1)
    return _jobs[job_id]


def test_upload_and_import(client):
    resp = client.post(
        "/import",
        data={"token": "tajemstvi", "network": "netvor-mpc", "label": "Netvor mPC"},
        files={"file": ("log-mpc.zip", _fixture_zip(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    job = _wait_for_job(client, resp.headers["location"])
    assert job["status"] == "done", job["lines"]
    assert any("47 rows imported" in ln for ln in job["lines"])  # importer ran

    # second upload of the same data: everything skipped, nothing duplicated
    resp2 = client.post(
        "/import",
        data={"token": "tajemstvi", "network": "netvor-mpc"},
        files={"file": ("log-mpc.zip", _fixture_zip(), "application/zip")},
        follow_redirects=False,
    )
    job2 = _wait_for_job(client, resp2.headers["location"])
    assert job2["status"] == "done"
    assert any("0 rows imported" in ln for ln in job2["lines"])


def test_wrong_token_rejected(client):
    resp = client.post(
        "/import",
        data={"token": "spatne", "network": "x"},
        files={"file": ("a.zip", _fixture_zip(), "application/zip")},
        follow_redirects=False,
    )
    assert resp.status_code == 403


def test_page_renders(client):
    assert client.get("/import").status_code == 200
