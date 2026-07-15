"""Denní reporty: textový souhrn do těla e-mailu + HTML příloha per síť.

Plánovač běží jako asyncio task (spí do NETMON_REPORT_HOUR v NETMON_TZ);
po startu dožene zmeškaný report přes meta.last_email_date (náhrada
Persistent=true starého systemd timeru).

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
from .timerange import day_bounds

log = logging.getLogger("netmon.report")


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s} s"


def _net_text(label: str, s: dict) -> str:
    lines = [f"== {label} =="]
    lines.append(f"{'cíl':<10} {'vzorků':>8} {'ztráta':>8} {'avg':>8} {'min':>8} {'max':>8}")
    for t in s["targets"]:
        avg = f"{t['avg']:.1f}" if t["avg"] is not None else "—"
        mn = f"{t['min']:.1f}" if t["min"] is not None else "—"
        mx = f"{t['max']:.1f}" if t["max"] is not None else "—"
        lines.append(f"{t['target']:<10} {t['samples']:>8} {t['loss']:>7.2f}% "
                     f"{avg:>8} {mn:>8} {mx:>8}")
    sp = s["speed"]
    if sp["n"]:
        lines.append(f"Rychlost: {sp['n']}× měřeno, avg {sp['avg']:.0f} Mbit/s "
                     f"(min {sp['min']:.0f} / max {sp['max']:.0f})")
    u = s["uptime"]
    if u["coverage"] is not None:
        lines.append(f"Pokrytí měření: {u['coverage']:.1f} % "
                     f"(mimo provoz {_fmt_dur(u['down_s'])}, přerušení {len(u['gaps'])}×)")
    es = s["events_summary"]
    if not es:
        lines.append("Výpadky: žádné 🎉")
    else:
        for scope, lbl in (("local", "lokální linka"), ("internet", "internet/ISP")):
            if scope in es:
                e = es[scope]
                lines.append(f"Výpadky ({lbl}): {e['count']}× · celkem {_fmt_dur(e['total_s'])} "
                             f"· nejdelší {_fmt_dur(e['longest_s'])} v {e['longest_at']}")
        for e in s["events"]:
            lines.append(f"  {e['start']} → {e['end'][11:19]}  {_fmt_dur(e['dur'])}  {e['note']}")
    return "\n".join(lines)


def _net_html(label: str, day: str, s: dict) -> str:
    """Samostatná HTML příloha — statické tabulky, bez JS."""
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
        for e in s["events"]) or '<tr><td colspan="4">Žádné výpadky 🎉</td></tr>'

    u = s["uptime"]
    gap_rows = "".join(
        f"<tr><td>{g['from']}</td><td>{g['to']}</td><td>{_fmt_dur(g['dur'])}</td>"
        f"<td>{'skript zastaven' if g['cause'] == 'stopped' else 'pád / vypnutý počítač'}</td></tr>"
        for g in u["gaps"]) or '<tr><td colspan="4">Bez přerušení 🎉</td></tr>'

    sp = s["speed"]
    speed_line = (f"{sp['n']}× měřeno, avg {sp['avg']:.0f} Mbit/s (min {sp['min']:.0f} / "
                  f"max {sp['max']:.0f})") if sp["n"] else "žádná měření"
    coverage_line = (f"{u['coverage']:.1f} % (mimo provoz {_fmt_dur(u['down_s'])})"
                     if u["coverage"] is not None else "—")

    th = 'style="text-align:left;padding:6px 10px;border-bottom:1px solid #334155;color:#94a3b8"'
    table = 'style="width:100%;border-collapse:collapse;font-size:14px"'
    return f"""<!DOCTYPE html><html lang="cs"><head><meta charset="utf-8">
<title>netmon — {html.escape(label)} — {day}</title></head>
<body style="margin:0;background:#0f172a;color:#e2e8f0;font:15px/1.5 system-ui,sans-serif;padding:24px">
<h1 style="font-size:22px">📡 {html.escape(label)} — {day}</h1>
<h2 style="font-size:16px">Latence a ztráty</h2>
<table {table}><tr><th {th}>cíl</th><th {th}>vzorků</th><th {th}>ztráta</th>
<th {th}>avg ms</th><th {th}>min</th><th {th}>max</th></tr>{rows}</table>
<h2 style="font-size:16px">Rychlost</h2><p>{speed_line}</p>
<h2 style="font-size:16px">Pokrytí měření</h2><p>{coverage_line}</p>
<table {table}><tr><th {th}>od</th><th {th}>do</th><th {th}>trvání</th><th {th}>příčina</th></tr>{gap_rows}</table>
<h2 style="font-size:16px">Výpadky</h2>
<table {table}><tr><th {th}>začátek</th><th {th}>konec</th><th {th}>trvání</th><th {th}>rozsah</th></tr>{ev_rows}</table>
<p style="color:#94a3b8;font-size:12px">netmon 2 · denní report</p>
</body></html>"""


def build_report(cfg: ServerConfig, day: datetime.date):
    """Vrací (subject, text, attachments) nebo None, když nejsou žádná data."""
    t0, t1 = day_bounds(day, cfg.tz)
    conn = connect(cfg.db_path)
    try:
        nets = conn.execute("SELECT * FROM networks ORDER BY name").fetchall()
        sections, attachments = [], []
        for net in nets:
            s = summary(conn, net["id"], t0, t1, cfg.ping_interval)
            if not s["targets"]:
                continue  # síť ten den nic nenaměřila
            sections.append(_net_text(net["label"], s))
            attachments.append((
                f"report-{net['name']}-{day.isoformat()}.html",
                _net_html(net["label"], day.isoformat(), s).encode("utf-8"),
                "text", "html",
            ))
    finally:
        conn.close()
    if not sections:
        return None
    subject = f"netmon report {day.isoformat()}"
    text = f"netmon — souhrn za {day.isoformat()}\n\n" + "\n\n".join(sections) + "\n"
    return subject, text, attachments


def send_daily_report(cfg: ServerConfig, day: datetime.date, out_dir: str = ".") -> bool:
    rep = build_report(cfg, day)
    if rep is None:
        log.info("Report %s: žádná data, nic neposílám.", day)
        return False
    subject, text, attachments = rep
    return send_email(subject, text, attachments, out_dir=out_dir)


async def report_scheduler(cfg: ServerConfig, stop: asyncio.Event) -> None:
    tz = ZoneInfo(cfg.tz)
    while not stop.is_set():
        now = datetime.datetime.now(tz)
        yesterday = now.date() - datetime.timedelta(days=1)

        # dožene zmeškaný report (start po 3:00, restart kontejneru…)
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
                log.exception("Odeslání denního reportu selhalo — zkusím to znovu za hodinu.")
                try:
                    await asyncio.wait_for(stop.wait(), timeout=3600)
                except asyncio.TimeoutError:
                    continue
                return

        # spánek do dalšího běhu (report_hour), kontrola stop po minutách
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
    ap = argparse.ArgumentParser(description="netmon denní report")
    ap.add_argument("--date", help="den YYYY-MM-DD (výchozí: včera)")
    ap.add_argument("--send", action="store_true", help="odeslat e-mailem (jinak jen vypsat)")
    ap.add_argument("--out", default=".", help="adresář pro SMTP_DRYRUN .eml / HTML")
    args = ap.parse_args()

    cfg = load_config()
    init_db(cfg.db_path)
    day = (datetime.date.fromisoformat(args.date) if args.date
           else datetime.date.today() - datetime.timedelta(days=1))

    rep = build_report(cfg, day)
    if rep is None:
        print(f"Za {day} nejsou žádná data.")
        return 1
    subject, text, attachments = rep
    print(text)
    if args.send:
        ok = send_email(subject, text, attachments, out_dir=args.out)
        print("Odesláno." if ok else "Neodesláno (chybí SMTP konfigurace).")
    else:
        for filename, data, *_ in attachments:
            path = f"{args.out}/{filename}"
            with open(path, "wb") as f:
                f.write(data)
            print(f"HTML uložen: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
