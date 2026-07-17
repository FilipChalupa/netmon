"""Email alerting: outage events and unreachable monitors.

Runs as an asyncio task on the evaluation server. Uses the same SMTP_*
variables as daily reports; without SMTP configured it stays idle.

- Outage alert: a derived outage event (local/internet) whose duration is
  ≥ NETMON_ALERT_MIN_OUTAGE_S (default 60 s) triggers one email. Ongoing
  outages keep their start time, so each event alerts exactly once —
  deduplicated via the alerts table (network_id, kind, key=start_epoch).
  Events found in one check are batched into a single email per network
  (a backfill after a long monitor gap doesn't flood the inbox).
- Monitor offline: sync hasn't succeeded for NETMON_ALERT_OFFLINE_S
  (default 600 s) → one email; recovery sends a follow-up and re-arms.
- Reach failure: NETMON_ALERT_REACH_FAILS consecutive reach probe FAILs
  (default 10 ≈ 5 min) → "pings work but the internet doesn't" (DNS or
  filtered traffic). Suppressed when a ping-derived outage overlaps the
  run, so a hard outage doesn't send two emails.
- Speed degradation: median of the recent tests (6 h window) below
  NETMON_ALERT_SPEED_PCT % (default 50) of the 30-day baseline median →
  one email; recovery (median back above threshold+20 points) sends a
  follow-up and re-arms. 0 disables.

Note: a monitor being offline usually just delays outage alerts — the
monitor keeps measuring locally and events are derived after backfill.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from .config import ServerConfig
from .db import connect
from .events import derive_events
from .mailer import send_email, smtp_configured

log = logging.getLogger("netmon.alerts")


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s} s"


def _already_sent(conn: sqlite3.Connection, network_id: int, kind: str, key: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM alerts WHERE network_id=? AND kind=? AND key=?",
        (network_id, kind, key)).fetchone() is not None


def _mark_sent(conn: sqlite3.Connection, network_id: int, kind: str, key: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO alerts(network_id, kind, key, sent_at) VALUES(?,?,?,?)",
        (network_id, kind, key, time.time()))
    conn.commit()


def _clear_sent(conn: sqlite3.Connection, network_id: int, kind: str, key: str) -> None:
    conn.execute("DELETE FROM alerts WHERE network_id=? AND kind=? AND key=?",
                 (network_id, kind, key))
    conn.commit()


def _check_outages(conn: sqlite3.Connection, cfg: ServerConfig, net, now: float,
                   events) -> list[str]:
    fresh = [e for e in events
             if e.duration_s >= cfg.alert_min_outage_s
             and not _already_sent(conn, net["id"], "outage", f"{e.start_epoch:.0f}")]
    if not fresh:
        return []

    worst = max(fresh, key=lambda e: e.duration_s)
    subject = (f"netmon ALERT: outage on {net['label']} "
               f"({worst.scope}, {_fmt_dur(worst.duration_s)})")
    lines = [f"Network: {net['label']} ({net['name']})", "", "Detected outages:"]
    lines += [f"  {e.start_iso} → {e.end_iso[11:19]}  {_fmt_dur(e.duration_s)}  {e.note}"
              for e in fresh]
    lines += ["", "An ongoing outage is reported once, with its duration so far."]
    if send_email(subject, "\n".join(lines)):
        for e in fresh:
            _mark_sent(conn, net["id"], "outage", f"{e.start_epoch:.0f}")
        return [subject]
    return []


def _check_reach(conn: sqlite3.Connection, cfg: ServerConfig, net, now: float,
                 events) -> list[str]:
    """Runs of consecutive reach FAILs — "pings work, the internet doesn't"."""
    rows = conn.execute(
        "SELECT ts_epoch, ts_iso, status FROM reach "
        "WHERE network_id=? AND ts_epoch>=? AND ts_epoch<=? ORDER BY ts_epoch",
        (net["id"], now - cfg.alert_lookback_s, now),
    ).fetchall()

    runs = []  # {"start", "start_iso", "end", "count"} of consecutive FAILs
    cur = None
    for r in rows:
        if r["status"] == "FAIL":
            if cur is None:
                cur = {"start": r["ts_epoch"], "start_iso": r["ts_iso"],
                       "end": r["ts_epoch"], "count": 1}
            else:
                cur["end"] = r["ts_epoch"]
                cur["count"] += 1
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)

    sent = []
    for run in runs:
        if run["count"] < cfg.alert_reach_fails:
            continue
        # a hard outage already alerts via _check_outages — don't double-report
        if any(e.start_epoch <= run["end"] and e.end_epoch >= run["start"]
               for e in events):
            continue
        key = f"{run['start']:.0f}"
        if _already_sent(conn, net["id"], "reach", key):
            continue
        subject = (f"netmon ALERT: internet failing on {net['label']} "
                   f"(reach probes down, pings OK)")
        body = (f"Network: {net['label']} ({net['name']})\n\n"
                f"{run['count']} consecutive reachability probes (DNS/TCP/TLS) "
                f"have failed since {run['start_iso']}, while pings still get "
                f"through.\nTypical causes: broken DNS or filtered/dropped "
                f"traffic at the provider.\n\n"
                f"An ongoing failure is reported once.")
        if send_email(subject, body):
            _mark_sent(conn, net["id"], "reach", key)
            sent.append(subject)
    return sent


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _check_speed(conn: sqlite3.Connection, cfg: ServerConfig, net, now: float) -> list[str]:
    """Sustained download-speed degradation against the 30-day baseline.

    Medians on both sides keep single bad tests (or a single lucky one)
    from flipping the state; recovery needs +20 points of headroom so a
    line hovering at the threshold doesn't flap emails."""
    if not cfg.alert_speed_pct:
        return []
    recent = [r["down_mbps"] for r in conn.execute(
        "SELECT down_mbps FROM speed WHERE network_id=? AND ts_epoch>? "
        "AND down_mbps IS NOT NULL",
        (net["id"], now - cfg.alert_speed_window_s))]
    baseline = [r["down_mbps"] for r in conn.execute(
        "SELECT down_mbps FROM speed WHERE network_id=? AND ts_epoch BETWEEN ? AND ? "
        "AND down_mbps IS NOT NULL",
        (net["id"], now - 30 * 86400, now - cfg.alert_speed_window_s))]
    if len(recent) < cfg.alert_speed_min_tests or \
            len(baseline) < cfg.alert_speed_min_baseline:
        return []

    cur, base = _median(recent), _median(baseline)
    pct = cur / base * 100 if base > 0 else 100.0
    degraded = pct < cfg.alert_speed_pct
    recovered = pct >= min(cfg.alert_speed_pct + 20, 90)
    alerted = _already_sent(conn, net["id"], "speed", "state")
    sent = []
    if degraded and not alerted:
        subject = (f"netmon ALERT: download speed degraded on {net['label']} "
                   f"({cur:.0f} Mbit/s, {pct:.0f}% of usual)")
        body = (f"Network: {net['label']} ({net['name']})\n\n"
                f"Median of the last {len(recent)} speed tests: {cur:.0f} Mbit/s\n"
                f"30-day baseline median: {base:.0f} Mbit/s\n"
                f"That is {pct:.0f}% of the usual speed "
                f"(alert threshold {cfg.alert_speed_pct}%).\n\n"
                f"A recovery email follows once the speed is back.")
        if send_email(subject, body):
            _mark_sent(conn, net["id"], "speed", "state")
            sent.append(subject)
    elif alerted and recovered:
        subject = (f"netmon: download speed on {net['label']} back to normal "
                   f"({cur:.0f} Mbit/s)")
        if send_email(subject, f"Median of the last {len(recent)} tests is "
                               f"{cur:.0f} Mbit/s ({pct:.0f}% of the 30-day "
                               f"baseline {base:.0f} Mbit/s)."):
            _clear_sent(conn, net["id"], "speed", "state")
            sent.append(subject)
    return sent


def _check_offline(conn: sqlite3.Connection, cfg: ServerConfig, net, now: float) -> list[str]:
    if not any(m.name == net["name"] for m in cfg.monitors):
        return []  # imported-only network, nothing to be offline
    st = conn.execute("SELECT * FROM sync_status WHERE network_id=?",
                      (net["id"],)).fetchone()
    if st is None or st["last_ok_at"] is None:
        return []  # never synced yet — installation in progress, not an alert
    offline = now - st["last_ok_at"] > cfg.alert_offline_s
    alerted = _already_sent(conn, net["id"], "offline", "state")
    sent = []
    if offline and not alerted:
        subject = f"netmon ALERT: monitor {net['label']} unreachable"
        body = (f"The monitor of network {net['label']} ({net['name']}) has not "
                f"synced for {_fmt_dur(now - st['last_ok_at'])}.\n"
                f"Last error: {st['last_error'] or '—'}\n\n"
                f"The monitor keeps measuring and buffering locally; data will be "
                f"backfilled once it is reachable again.")
        if send_email(subject, body):
            _mark_sent(conn, net["id"], "offline", "state")
            sent.append(subject)
    elif not offline and alerted:
        subject = f"netmon: monitor {net['label']} back online"
        if send_email(subject, f"The monitor of network {net['label']} "
                               f"({net['name']}) is syncing again."):
            _clear_sent(conn, net["id"], "offline", "state")
            sent.append(subject)
    return sent


def check_once(conn: sqlite3.Connection, cfg: ServerConfig,
               now: float | None = None) -> list[str]:
    """One alerting pass over all networks. Returns sent subjects (for tests)."""
    now = now if now is not None else time.time()
    sent: list[str] = []
    for net in conn.execute("SELECT * FROM networks ORDER BY name").fetchall():
        try:
            events = derive_events(conn, net["id"], now - cfg.alert_lookback_s,
                                   now, cfg.ping_interval)
            sent += _check_outages(conn, cfg, net, now, events)
            sent += _check_reach(conn, cfg, net, now, events)
            sent += _check_speed(conn, cfg, net, now)
            sent += _check_offline(conn, cfg, net, now)
        except Exception:
            log.exception("alert check failed for %s", net["name"])
    return sent


async def alert_loop(cfg: ServerConfig, stop: asyncio.Event) -> None:
    if not cfg.alerts_enabled:
        log.info("Alerting disabled (NETMON_ALERTS=0).")
        return
    conn = connect(cfg.db_path)
    try:
        while not stop.is_set():
            if smtp_configured():
                sent = await asyncio.to_thread(check_once, conn, cfg)
                for s in sent:
                    log.info("alert sent: %s", s)
            try:
                await asyncio.wait_for(stop.wait(), timeout=cfg.alert_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        conn.close()
