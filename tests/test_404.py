"""404 handling: browsers get a styled HTML page, API callers keep JSON."""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from netmon_server.main import app  # noqa: E402


@pytest.fixture
def client():
    # no lifespan on purpose: the handler must survive app.state.cfg missing
    return TestClient(app, raise_server_exceptions=False)


def test_browser_404_is_html(client):
    r = client.get("/totally/unknown", headers={"Accept": "text/html,*/*"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("text/html")
    assert "404" in r.text and "/totally/unknown" in r.text


def test_api_404_stays_json(client):
    r = client.get("/api/totally/unknown", headers={"Accept": "text/html,*/*"})
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json() == {"detail": "Not Found"}


def test_non_browser_404_stays_json(client):
    r = client.get("/totally/unknown", headers={"Accept": "application/json"})
    assert r.status_code == 404
    assert r.json() == {"detail": "Not Found"}
