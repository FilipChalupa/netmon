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


def _check_outages(conn: sqlite3.Connection, cfg: ServerConfig, net, now: float) -> list[str]:
    events = derive_events(conn, net["id"], now - cfg.alert_lookback_s, now,
                           cfg.ping_interval)
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
            sent += _check_outages(conn, cfg, net, now)
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
