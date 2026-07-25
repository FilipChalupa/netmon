"""Daily reports: text summary in the email body + one HTML attachment per network.

The scheduler runs as an asyncio task (sleeps until NETMON_REPORT_HOUR in
NETMON_TZ); after startup it catches up on a missed report via
meta.last_email_date (replacement for the old systemd timer's Persistent=true).

CLI:  python -m netmon_server.report --date 2026-07-14 [--send] [--out DIR]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import html
import logging
import sys
from zoneinfo import ZoneInfo

from .aggregate import summary
from .config import ServerConfig, load_config
from .db import connect, get_meta, init_db, set_meta
from .mailer import send_email, smtp_configured
from .notes import list_notes
from .timerange import day_bounds

log = logging.getLogger("netmon.report")


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s} s"


def _fmt_notes(notes: list[dict], tz: ZoneInfo) -> list[dict]:
    """Notes as display rows: local HH:MM, text, scope label."""
    return [{
        "when": datetime.datetime.fromtimestamp(n["ts_epoch"], tz).strftime("%H:%M"),
        "text": n["text"],
        "scope": ", ".join(w["label"] for w in n["networks"]) or "general",
    } for n in notes]


def _net_text(label: str, s: dict, notes: list[dict] = ()) -> str:
    lines = [f"== {label} =="]
    lines.append(f"{'target':<10} {'samples':>8} {'loss':>8} {'avg':>8} {'min':>8} {'max':>8}")
    for t in s["targets"]:
        avg = f"{t['avg']:.1f}" if t["avg"] is not None else "—"
        mn = f"{t['min']:.1f}" if t["min"] is not None else "—"
        mx = f"{t['max']:.1f}" if t["max"] is not None else "—"
        lines.append(f"{t['target']:<10} {t['samples']:>8} {t['loss']:>7.2f}% "
                     f"{avg:>8} {mn:>8} {mx:>8}")
    sp = s["speed"]
    if sp["n"]:
        line = (f"Speed: {sp['n']} tests, down avg {sp['avg']:.0f} Mbit/s "
                f"(min {sp['min']:.0f} / max {sp['max']:.0f})")
        if sp.get("up_avg") is not None:
            line += (f", up avg {sp['up_avg']:.0f} Mbit/s "
                     f"(min {sp['up_min']:.0f} / max {sp['up_max']:.0f})")
        lines.append(line)
        if sp.get("bloat_avg") is not None:
            lines.append(f"Bufferbloat: latency under load avg +{sp['bloat_avg']:.0f} ms "
                         f"(worst +{sp['bloat_max']:.0f} ms)")
    pub = s.get("pubip") or {}
    if pub.get("current"):
        cur = pub["current"]
        line = f"Public IP: {cur['ip']}"
        if cur.get("ptr"):
            line += f" ({cur['ptr']})"
        if pub["changes"]:
            line += f" · changed {len(pub['changes'])}× this day"
        lines.append(line)
    u = s["uptime"]
    if u["coverage"] is not None:
        lines.append(f"Measurement coverage: {u['coverage']:.1f} % "
                     f"(downtime {_fmt_dur(u['down_s'])}, interruptions {len(u['gaps'])}×)")
    es = s["events_summary"]
    if not es:
        lines.append("Outages: none 🎉")
    else:
        for scope, lbl in (("local", "local link"), ("internet", "internet/ISP"),
                           ("reach", "reach/DNS")):
            if scope in es:
                e = es[scope]
                lines.append(f"Outages ({lbl}): {e['count']}× · total {_fmt_dur(e['total_s'])} "
                             f"· longest {_fmt_dur(e['longest_s'])} at {e['longest_at']}")
        for e in s["events"]:
            lines.append(f"  {e['start']} → {e['end'][11:19]}  {_fmt_dur(e['dur'])}  {e['note']}")
    if notes:
        lines.append("Notes:")
        for n in notes:
            lines.append(f"  {n['when']}  {n['text']}  ({n['scope']})")
    return "\n".join(lines)


def _net_html(label: str, day: str, s: dict, notes: list[dict] = ()) -> str:
    """Standalone HTML attachment — static tables, no JS."""
    def pill(loss):
        color = "#ef4444" if loss > 1 else "#f59e0b" if loss > 0.1 else "#22c55e"
        return f'<span style="color:{color};font-weight:600">{loss:.2f}%</span>'

    rows = "".join(
        f"<tr><td>{html.escape(t['target'])}</td><td>{t['samples']}</td>"
        f"<td>{pill(t['loss'])}</td>"
        f"<td>{t['avg'] if t['avg'] is not None else '—'}</td>"
        f"<td>{t['min'] if t['min'] is not None else '—'}</td>"
        f"<td>{t['max'] if t['max'] is not None else '—'}</td></tr>"
        for t in s["targets"])

    ev_rows = "".join(
        f"<tr><td>{e['start']}</td><td>{e['end']}</td><td>{_fmt_dur(e['dur'])}</td>"
        f"<td>{html.escape(e['note'])}</td></tr>"
        for e in s["events"]) or '<tr><td colspan="4">No outages 🎉</td></tr>'

    u = s["uptime"]
    gap_rows = "".join(
        f"<tr><td>{g['from']}</td><td>{g['to']}</td><td>{_fmt_dur(g['dur'])}</td>"
        f"<td>{'script stopped' if g['cause'] == 'stopped' else 'crash / powered-off host'}</td></tr>"
        for g in u["gaps"]) or '<tr><td colspan="4">No interruptions 🎉</td></tr>'

    sp = s["speed"]
    speed_line = (f"{sp['n']} tests, ⬇ avg {sp['avg']:.0f} Mbit/s (min {sp['min']:.0f} / "
                  f"max {sp['max']:.0f})") if sp["n"] else "no measurements"
    if sp["n"] and sp.get("up_avg") is not None:
        speed_line += (f" · ⬆ avg {sp['up_avg']:.0f} Mbit/s (min {sp['up_min']:.0f} / "
                       f"max {sp['up_max']:.0f})")
    if sp["n"] and sp.get("bloat_avg") is not None:
        speed_line += (f" · bufferbloat avg +{sp['bloat_avg']:.0f} ms "
                       f"(worst +{sp['bloat_max']:.0f} ms)")
    coverage_line = (f"{u['coverage']:.1f} % (downtime {_fmt_dur(u['down_s'])})"
                     if u["coverage"] is not None else "—")
    pub = s.get("pubip") or {}
    pubip_line = ""
    if pub.get("current"):
        cur = pub["current"]
        pubip_line = f"<p>{html.escape(cur['ip'])}"
        if cur.get("ptr"):
            pubip_line += f" · {html.escape(cur['ptr'])}"
        if pub["changes"]:
            pubip_line += f" · changed {len(pub['changes'])}× this day"
        pubip_line = ('<h2 style="font-size:16px">Public IP</h2>' + pubip_line + "</p>")

    th = 'style="text-align:left;padding:6px 10px;border-bottom:1px solid #334155;color:#94a3b8"'
    table = 'style="width:100%;border-collapse:collapse;font-size:14px"'

    notes_html = ""
    if notes:
        note_rows = "".join(
            f"<tr><td>{n['when']}</td><td>{html.escape(n['text'])}</td>"
            f"<td>{html.escape(n['scope'])}</td></tr>"
            for n in notes)
        notes_html = (f'<h2 style="font-size:16px">Notes</h2>'
                      f"<table {table}><tr><th {th}>when</th><th {th}>note</th>"
                      f"<th {th}>networks</th></tr>{note_rows}</table>")

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>netmon — {html.escape(label)} — {day}</title></head>
<body style="margin:0;background:#0f172a;color:#e2e8f0;font:15px/1.5 system-ui,sans-serif;padding:24px">
<h1 style="font-size:22px">📡 {html.escape(label)} — {day}</h1>
<h2 style="font-size:16px">Latency and loss</h2>
<table {table}><tr><th {th}>target</th><th {th}>samples</th><th {th}>loss</th>
<th {th}>avg ms</th><th {th}>min</th><th {th}>max</th></tr>{rows}</table>
<h2 style="font-size:16px">Speed</h2><p>{speed_line}</p>
{pubip_line}
<h2 style="font-size:16px">Measurement coverage</h2><p>{coverage_line}</p>
<table {table}><tr><th {th}>from</th><th {th}>to</th><th {th}>duration</th><th {th}>cause</th></tr>{gap_rows}</table>
<h2 style="font-size:16px">Outages</h2>
<table {table}><tr><th {th}>start</th><th {th}>end</th><th {th}>duration</th><th {th}>scope</th></tr>{ev_rows}</table>
{notes_html}
<p style="color:#94a3b8;font-size:12px">netmon 2 · daily report</p>
</body></html>"""


def build_report(cfg: ServerConfig, day: datetime.date):
    """Returns (subject, text, attachments), or None when there is no data."""
    t0, t1 = day_bounds(day, cfg.tz)
    tz = ZoneInfo(cfg.tz)
    conn = connect(cfg.db_path)
    try:
        nets = conn.execute("SELECT * FROM networks ORDER BY name").fetchall()
        sections, attachments = [], []
        for net in nets:
            s = summary(conn, net["id"], t0, t1, cfg.ping_interval,
                        cfg.alert_reach_fails)
            if not s["targets"]:
                continue  # this network measured nothing that day
            notes = _fmt_notes(list_notes(conn, t0, t1, [net["name"]]), tz)
            sections.append(_net_text(net["label"], s, notes))
            attachments.append((
                f"report-{net['name']}-{day.isoformat()}.html",
                _net_html(net["label"], day.isoformat(), s, notes).encode("utf-8"),
                "text", "html",
            ))
    finally:
        conn.close()
    if not sections:
        return None
    subject = f"netmon report {day.isoformat()}"
    text = f"netmon — summary for {day.isoformat()}\n\n" + "\n\n".join(sections) + "\n"
    return subject, text, attachments


def _wk_stats(s: dict) -> dict:
    """Digest metrics from a summary(): worst public loss, mean public
    latency, speed/bloat averages, outage totals, coverage."""
    pubs = [t for t in s["targets"] if t["target"] != "gateway"]
    lats = [t["avg"] for t in pubs if t["avg"] is not None]
    es = s["events_summary"] or {}
    return {
        "loss": max((t["loss"] for t in pubs), default=None),
        "lat": sum(lats) / len(lats) if lats else None,
        "down": s["speed"]["avg"],
        "up": s["speed"].get("up_avg"),
        "bloat": s["speed"].get("bloat_avg"),
        "out_n": sum(e["count"] for e in es.values()),
        "out_s": sum(e["total_s"] for e in es.values()),
        "cov": s["uptime"]["coverage"],
    }


def _wk_line(label: str, cur, prev, fmt, pct_delta: bool = False) -> str:
    def f(v):
        return fmt.format(v) if v is not None else "—"
    line = f"  {label:<17} {f(cur):>12}   (prev {f(prev)}"
    if pct_delta and cur is not None and prev:
        line += f", {(cur - prev) / prev * 100:+.0f} %"
    return line + ")"


def build_weekly(cfg: ServerConfig, monday: datetime.date):
    """(subject, text) for the week starting `monday`, with the previous
    week alongside every number. Returns None when nothing measured."""
    t0, _ = day_bounds(monday, cfg.tz)
    _, t1 = day_bounds(monday + datetime.timedelta(days=6), cfg.tz)
    p0, _ = day_bounds(monday - datetime.timedelta(days=7), cfg.tz)
    sunday = monday + datetime.timedelta(days=6)

    conn = connect(cfg.db_path)
    try:
        sections = []
        for net in conn.execute("SELECT * FROM networks ORDER BY name").fetchall():
            cur_s = summary(conn, net["id"], t0, t1, cfg.ping_interval,
                            cfg.alert_reach_fails)
            prev_s = summary(conn, net["id"], p0, t0, cfg.ping_interval,
                             cfg.alert_reach_fails)
            if not cur_s["targets"] and not prev_s["targets"]:
                continue
            cur, prev = _wk_stats(cur_s), _wk_stats(prev_s)
            lines = [f"== {net['label']} =="]
            if net["description"]:
                lines.append(f"  ({net['description']})")
            lines += [
                _wk_line("internet loss:", cur["loss"], prev["loss"], "{:.2f} %"),
                _wk_line("latency avg:", cur["lat"], prev["lat"], "{:.1f} ms"),
                _wk_line("download avg:", cur["down"], prev["down"],
                         "{:.0f} Mbit/s", pct_delta=True),
                _wk_line("upload avg:", cur["up"], prev["up"],
                         "{:.0f} Mbit/s", pct_delta=True),
                _wk_line("bufferbloat avg:", cur["bloat"], prev["bloat"], "+{:.0f} ms"),
                f"  {'outages:':<17} {cur['out_n']}× / {_fmt_dur(cur['out_s'])}"
                f"   (prev {prev['out_n']}× / {_fmt_dur(prev['out_s'])})",
                _wk_line("coverage:", cur["cov"], prev["cov"], "{:.1f} %"),
            ]
            sections.append("\n".join(lines))
    finally:
        conn.close()
    if not sections:
        return None
    subject = f"netmon weekly digest {monday.isoformat()} – {sunday.isoformat()}"
    text = (f"netmon — week {monday.isoformat()} → {sunday.isoformat()} "
            f"(previous week in brackets)\n\n" + "\n\n".join(sections) + "\n")
    return subject, text


def last_complete_week_monday(today: datetime.date) -> datetime.date:
    return today - datetime.timedelta(days=today.weekday() + 7)


def weekly_due(last_sent_monday: str | None,
               today: datetime.date) -> datetime.date | None:
    """Monday of the week to send now, or None when already sent."""
    monday = last_complete_week_monday(today)
    if last_sent_monday is None or \
            datetime.date.fromisoformat(last_sent_monday) < monday:
        return monday
    return None


def send_weekly_digest(cfg: ServerConfig, monday: datetime.date,
                       out_dir: str = ".") -> bool:
    rep = build_weekly(cfg, monday)
    if rep is None:
        log.info("Weekly digest %s: no data, not sending anything.", monday)
        return False
    subject, text = rep
    return send_email(subject, text, out_dir=out_dir)


def send_daily_report(cfg: ServerConfig, day: datetime.date, out_dir: str = ".") -> bool:
    rep = build_report(cfg, day)
    if rep is None:
        log.info("Report %s: no data, not sending anything.", day)
        return False
    subject, text, attachments = rep
    return send_email(subject, text, attachments, out_dir=out_dir)


async def report_scheduler(cfg: ServerConfig, stop: asyncio.Event) -> None:
    tz = ZoneInfo(cfg.tz)
    while not stop.is_set():
        now = datetime.datetime.now(tz)
        yesterday = now.date() - datetime.timedelta(days=1)

        # catch up on a missed report (start after 3:00, container restart…)
        conn = connect(cfg.db_path)
        try:
            last = get_meta(conn, "last_email_date")
        finally:
            conn.close()
        due = last is None or datetime.date.fromisoformat(last) < yesterday
        if due and now.hour >= cfg.report_hour and smtp_configured():
            try:
                await asyncio.to_thread(send_daily_report, cfg, yesterday)
                conn = connect(cfg.db_path)
                try:
                    set_meta(conn, "last_email_date", yesterday.isoformat())
                finally:
                    conn.close()
            except Exception:
                log.exception("Sending the daily report failed — retrying in an hour.")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    continue
                return

        # weekly digest: once the last complete week (Mon–Sun) is behind us
        if cfg.weekly_enabled and now.hour >= cfg.report_hour and smtp_configured():
            conn = connect(cfg.db_path)
            try:
                monday = weekly_due(get_meta(conn, "last_weekly_date"), now.date())
            finally:
                conn.close()
            if monday is not None:
                try:
                    await asyncio.to_thread(send_weekly_digest, cfg, monday)
                    conn = connect(cfg.db_path)
                    try:
                        set_meta(conn, "last_weekly_date", monday.isoformat())
                    finally:
                        conn.close()
                except Exception:
                    log.exception("Sending the weekly digest failed — will retry "
                                  "on the next scheduler pass.")

        # sleep until the next run (report_hour), checking stop along the way
        target = datetime.datetime.combine(
            now.date() + datetime.timedelta(days=0 if now.hour < cfg.report_hour else 1),
            datetime.time(cfg.report_hour), tz)
        wait = max((target - datetime.datetime.now(tz)).total_seconds(), 60)
        try:
            await asyncio.wait_for(stop.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="netmon daily report / weekly digest")
    ap.add_argument("--date", help="day YYYY-MM-DD (default: yesterday); "
                                   "with --weekly: any day of the wanted week")
    ap.add_argument("--weekly", action="store_true",
                    help="weekly digest instead of the daily report")
    ap.add_argument("--send", action="store_true", help="send by email (otherwise just print)")
    ap.add_argument("--out", default=".", help="directory for SMTP_DRYRUN .eml / HTML files")
    args = ap.parse_args()

    cfg = load_config()
    init_db(cfg.db_path)
    day = (datetime.date.fromisoformat(args.date) if args.date
           else datetime.date.today() - datetime.timedelta(days=1))

    if args.weekly:
        monday = day - datetime.timedelta(days=day.weekday())
        rep = build_weekly(cfg, monday)
        if rep is None:
            print(f"No data for the week of {monday}.")
            return 1
        subject, text = rep
        print(text)
        if args.send:
            ok = send_email(subject, text, out_dir=args.out)
            print("Sent." if ok else "Not sent (SMTP configuration missing).")
        return 0

    rep = build_report(cfg, day)
    if rep is None:
        print(f"No data for {day}.")
        return 1
    subject, text, attachments = rep
    print(text)
    if args.send:
        ok = send_email(subject, text, attachments, out_dir=args.out)
        print("Sent." if ok else "Not sent (SMTP configuration missing).")
    else:
        for filename, data, *_ in attachments:
            path = f"{args.out}/{filename}"
            with open(path, "wb") as f:
                f.write(data)
            print(f"HTML saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
