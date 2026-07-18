"""E2E frontend tests (Playwright): the regressions pytest can't see.

Covers the interactions that broke silently in the past: note-marker hover
tooltips, drag-to-zoom navigation, and the add-note roundtrip. Skipped when
playwright (or its browser) isn't available.
"""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
pytest.importorskip("fastapi")

from playwright.sync_api import Error as PWError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from netmon_server.db import connect, get_or_create_network, init_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """uvicorn subprocess over a seeded DB; yields the base URL."""
    tmp = tmp_path_factory.mktemp("frontend")
    db_path = str(tmp / "server.db")
    init_db(db_path)
    conn = connect(db_path)
    nid = get_or_create_network(conn, "e2e", "E2E net")
    now = time.time()
    t = now - 3600
    while t < now:  # an hour of pings with a 2-min internet outage in the middle
        down = now - 1900 <= t <= now - 1780
        for tgt in ("gateway", "quad9", "google"):
            lost = down and tgt != "gateway"
            conn.execute(
                "INSERT INTO latency(network_id, ts_epoch, ts_iso, target, status, "
                "rtt_ms) VALUES(?,?,?,?,?,?)",
                (nid, t, "", tgt, "LOSS" if lost else "OK", None if lost else 10.0))
        t += 4
    conn.execute("INSERT INTO notes(ts_epoch, text, created_at) VALUES(?,?,?)",
                 (now - 1200, "seeded marker note", now))
    conn.commit()
    conn.close()

    port = _free_port()
    # the subprocess doesn't inherit conftest's sys.path tweaks — put the
    # package roots on PYTHONPATH explicitly
    pypath = os.pathsep.join(
        [str(ROOT / "server"), str(ROOT / "monitor")]
        + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
    env = dict(os.environ, NETMON_DB=db_path, NETMON_MONITORS=str(tmp / "no.toml"),
               NETMON_ALERTS="0", PYTHONPATH=pypath)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "netmon_server.main:app",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, cwd=str(tmp),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    try:
        import urllib.request
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/health", timeout=1)
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.skip("test server did not start")
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture(scope="module")
def page(server):
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pg = browser.new_page()
            yield pg
            browser.close()
    except PWError as e:
        pytest.skip(f"chromium unavailable: {e}")


def _open_network(page, server):
    page.goto(server + "/net/e2e")
    page.wait_for_function("!!Chart.getChart(document.getElementById('latChart'))")
    # the chart sits below the fold; mouse coordinates are viewport-relative
    page.locator("#latChart").scroll_into_view_if_needed()


def test_dashboard_renders_cards(page, server):
    page.goto(server + "/")
    page.wait_for_selector(".card h3")
    assert "e2e net" in page.inner_text("#netcards").lower()


def test_outage_table_and_chart_render(page, server):
    _open_network(page, server)
    page.wait_for_selector("#events table")
    assert "internet / ISP" in page.inner_text("#events")
    # outage start links to a zoomed custom range
    href = page.get_attribute("#events a", "href")
    assert "range=custom" in href and "from=" in href


def test_note_marker_hover_shows_tooltip(page, server):
    """Regression: the tooltip never appeared (object identity lost on redraw)."""
    _open_network(page, server)
    box = page.eval_on_selector("#latChart", """el => {
      const chart = Chart.getChart(el);
      const marks = chart.$noteXs || [];
      const r = el.getBoundingClientRect();
      return marks.length ? {x: r.left + marks[0].px, y: r.top + r.height / 2} : null;
    }""")
    assert box, "note marker was not drawn"
    page.mouse.move(box["x"] - 10, box["y"])
    page.mouse.move(box["x"], box["y"], steps=3)
    page.wait_for_function(
        "!!Chart.getChart(document.getElementById('latChart')).$noteHover")
    hover = page.evaluate(
        "Chart.getChart(document.getElementById('latChart')).$noteHover.text")
    assert hover == "seeded marker note"


def test_drag_zoom_navigates_to_custom_range(page, server):
    _open_network(page, server)
    r = page.eval_on_selector("#latChart", """el => {
      const a = Chart.getChart(el).chartArea, b = el.getBoundingClientRect();
      return {x0: b.left + a.left + 40, x1: b.left + a.left + 220,
              y: b.top + (a.top + a.bottom) / 2};
    }""")
    page.mouse.move(r["x0"], r["y"])
    page.mouse.down()
    page.mouse.move(r["x1"], r["y"], steps=5)
    page.mouse.up()
    page.wait_for_url("**range=custom**")
    assert "from=" in page.url and "to=" in page.url


def test_add_note_roundtrip(page, server):
    _open_network(page, server)
    page.fill("#noteText", "note added by playwright")
    page.click("#noteForm button[type=submit]")
    # the form POSTs then reloads — wait for the note to show up in the list
    page.wait_for_selector("#noteList >> text=note added by playwright")
