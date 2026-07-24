"""Generate the README screenshots from seeded demo data.

Spins up the evaluation server over a throwaway SQLite DB filled with a
realistic day (three networks, an ISP outage, a bufferbloat spike, a note,
a year of heatmap history) and captures the pages with Playwright.

Run from the repo root:  python docs/screenshots/generate.py
Needs: server requirements + playwright (chromium installed).
"""

from __future__ import annotations

import datetime
import os
import random
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "monitor"))

from netmon_server.db import connect, get_or_create_network, init_db  # noqa: E402

TZ = ZoneInfo("Europe/Prague")
rng = random.Random(42)

NETS = {
    "home":    {"label": "Home (fiber)",    "gw": 1.2, "pub": 8.0,  "down": 520, "idle": 8},
    "cottage": {"label": "Cottage (LTE)",   "gw": 2.5, "pub": 42.0, "down": 55,  "idle": 40},
    "parents": {"label": "Parents (DSL)",   "gw": 1.8, "pub": 16.0, "down": 95,  "idle": 14},
}
# ISP outage on home: both public targets dark, gateway fine
OUTAGE_H = (14 * 3600 + 2 * 60, 14 * 3600 + 5 * 60 + 20)


def seed(db_path: str) -> None:
    init_db(db_path)
    conn = connect(db_path)
    now = time.time()
    midnight = datetime.datetime.now(TZ).replace(hour=0, minute=0, second=0,
                                                 microsecond=0).timestamp()
    end = min(midnight + 22 * 3600, now)

    def iso(t: float) -> str:
        return datetime.datetime.fromtimestamp(t, TZ).isoformat(timespec="seconds")

    for name, p in NETS.items():
        nid = get_or_create_network(conn, name, p["label"])
        lat, reach, beats, speed = [], [], [], []

        def rtt(base, t):
            evening = 1 + 0.25 * max(0, ((t - midnight) / 3600 - 17) / 5)  # peak-hour drift
            return round(base * evening * rng.uniform(0.85, 1.25), 2)

        outages = [OUTAGE_H] if name == "home" else \
                  [(9 * 3600 + 11 * 60, 9 * 3600 + 12 * 60),
                   (16 * 3600 + 40 * 60, 16 * 3600 + 41 * 60)] if name == "cottage" else []

        t = midnight
        while t < end:  # real ping cadence so daily loss percentages stay honest
            off = t - midnight
            in_outage = any(a <= off < b for a, b in outages)
            ts_iso = iso(t)
            lat.append((nid, t, ts_iso, "gateway", "ok", rtt(p["gw"], t)))
            lossy = name == "cottage" and rng.random() < 0.004
            for tgt in ("quad9", "google"):
                if in_outage or (lossy and tgt == "quad9"):
                    lat.append((nid, t, ts_iso, tgt, "LOSS", None))
                else:
                    lat.append((nid, t, ts_iso, tgt, "ok", rtt(p["pub"], t)))
            if int(off) % 300 == 0:  # reach every 5 min
                if in_outage:
                    reach.append((nid, t, ts_iso, None, None, None, 0, "FAIL"))
                else:
                    reach.append((nid, t, ts_iso, round(rng.uniform(3, 9), 1),
                                  round(rng.uniform(5, 14), 1),
                                  round(rng.uniform(22, 38), 1), 204, "ok"))
            t += 2

        t = midnight
        while t < end:  # heartbeats; cottage crashed for 25 min at 07:30
            if not (name == "cottage" and 7.5 * 3600 <= t - midnight < 7.5 * 3600 + 1500):
                beats.append((nid, t, iso(t), "ALIVE"))
            t += 60

        h = 0
        while midnight + h * 3600 + 1800 < end:  # hourly speed tests at :30
            ts = midnight + h * 3600 + 1800
            dip = 0.82 if 19 <= h <= 21 else 1.0
            down = round(p["down"] * dip * rng.uniform(0.93, 1.05), 1)
            up = round(down * rng.uniform(0.09, 0.12) * 10) / 10 if name != "cottage" \
                else round(down * 0.35, 1)
            idle = round(p["idle"] * rng.uniform(0.9, 1.15), 1)
            loaded = round(idle + rng.uniform(6, 18), 1)
            if name == "home" and h == 12:
                loaded = round(idle + 122, 1)  # lunchtime bufferbloat spike
            if name == "cottage":
                loaded = round(idle + rng.uniform(35, 80), 1)
            speed.append((nid, ts, iso(ts), down, 50_000_000, 1.0, 200, up, idle, loaded))
            h += 1

        if name == "home":  # a year of hourly history for the heatmap
            bad_days = {rng.randrange(365) for _ in range(14)}
            for d in range(365, 0, -1):
                day0 = midnight - d * 86400
                bad = d in bad_days
                for hh in range(24):
                    for tgt in ("quad9", "google"):
                        lost = bad and rng.random() < rng.uniform(0.05, 0.2)
                        lat.append((nid, day0 + hh * 3600, "", tgt,
                                    "LOSS" if lost else "ok",
                                    None if lost else rtt(p["pub"], day0)))

        conn.executemany("INSERT INTO latency(network_id, ts_epoch, ts_iso, target, "
                         "status, rtt_ms) VALUES(?,?,?,?,?,?)", lat)
        conn.executemany("INSERT INTO reach(network_id, ts_epoch, ts_iso, dns_ms, "
                         "tcp_ms, tls_ms, http_code, status) VALUES(?,?,?,?,?,?,?,?)", reach)
        conn.executemany("INSERT INTO uptime(network_id, ts_epoch, ts_iso, event) "
                         "VALUES(?,?,?,?)", beats)
        conn.executemany("INSERT INTO speed(network_id, ts_epoch, ts_iso, down_mbps, "
                         "bytes, seconds, http_code, up_mbps, idle_rtt_ms, loaded_rtt_ms) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)", speed)
        conn.execute("INSERT INTO pubip(network_id, ts_epoch, ts_iso, ip) VALUES(?,?,?,?)",
                     (nid, midnight, iso(midnight),
                      {"home": "203.0.113.42", "cottage": "100.72.14.3",
                       "parents": "198.51.100.17"}[name]))

    conn.execute("INSERT INTO notes(ts_epoch, text, created_at) VALUES(?,?,?)",
                 (midnight + OUTAGE_H[0], "ISP confirmed a maintenance window", now))
    conn.commit()
    conn.close()


def clip_of(page, first_sel: str, last_sel: str) -> dict:
    a = page.locator(first_sel).bounding_box()
    b = page.locator(last_sel).bounding_box()
    return {"x": 0, "y": a["y"] - 8, "width": 1360,
            "height": b["y"] + b["height"] - a["y"] + 16}


def shoot(base: str) -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1360, "height": 880},
                                device_scale_factor=2)

        page.goto(base + "/")
        page.wait_for_selector("#netcards .card")
        page.wait_for_timeout(1500)  # sparklines
        page.screenshot(path=str(HERE / "dashboard.png"), full_page=True)

        page.goto(base + "/net/home")
        page.wait_for_function(
            "!!Chart.getChart(document.getElementById('bloatChart'))")
        page.wait_for_timeout(800)
        page.screenshot(path=str(HERE / "network-detail.png"), full_page=True,
                        clip=clip_of(page, "nav", "#latChart"))
        page.screenshot(path=str(HERE / "speed-bufferbloat.png"), full_page=True,
                        clip=clip_of(page, "#spdChart", "#bloatChart"))
        page.wait_for_selector(".hm-grid")
        page.screenshot(path=str(HERE / "heatmap.png"), full_page=True,
                        clip=clip_of(page, "#heatmap", "#heatmap"))

        page.goto(base + "/compare")
        page.wait_for_function(
            "!!Chart.getChart(document.getElementById('cmpSpd'))")
        page.wait_for_timeout(800)
        page.screenshot(path=str(HERE / "compare.png"), full_page=True,
                        clip=clip_of(page, "#cmpLat", "#cmpSpd"))
        browser.close()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "server.db")
        seed(db)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        pypath = os.pathsep.join([str(ROOT / "server"), str(ROOT / "monitor")])
        env = dict(os.environ, NETMON_DB=db, NETMON_MONITORS=os.path.join(tmp, "no.toml"),
                   NETMON_ALERTS="0", PYTHONPATH=pypath)
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "netmon_server.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            env=env, cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(50):
                try:
                    urllib.request.urlopen(base + "/api/health", timeout=1)
                    break
                except OSError:
                    time.sleep(0.2)
            shoot(base)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    for f in sorted(HERE.glob("*.png")):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
