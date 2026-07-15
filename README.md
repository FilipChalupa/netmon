# netmon 2 — connection quality monitoring across networks

A two-part Python system for long-term measurement of connection quality
(**outages, latency, jitter, speed, service reachability**) in several
networks at once, with central evaluation and web charts.

```
┌─ network A (home) ──────┐        ┌─ anywhere on the internet (Coolify) ──┐
│ monitor (Python stdlib) │◄───────┤ evaluation server (FastAPI + SQLite)  │
│ ping/reach/speed→SQLite │  pull  │  · continuous sync from monitors      │
│ mini HTTP API :8787     │  over  │  · web: dashboard, detail, comparison │
└─────────────────────────┘  Tail- │  · outage derivation, coverage        │
┌─ network B (cottage) ───┐  scale │  · daily email reports                │
│ monitor                 │◄───────┤                                       │
└─────────────────────────┘        └───────────────────────────────────────┘
```

- **`monitor/`** — measuring agent. Pure Python with **no dependencies**
  (just `python3` and `ping`; nothing to install on Ubuntu). It measures,
  stores locally in SQLite and exposes a mini HTTP API the server pulls
  from. The monitor buffers data — if the server is down or the network
  drops, everything is backfilled once the connection returns.
- **`server/`** — evaluation server. Pulls data from all monitors
  (incrementally, idempotently), stores it in a single SQLite database and
  serves a web frontend (dark theme): network dashboard, detail with
  latency / loss / DNS-TCP-TLS / speed charts, outage table, measurement
  coverage, and an **overlay comparison of networks**. Sends daily email
  reports.
- **`legacy/`** — the original bash version (single host, CSV + HTML
  report). Kept as reference; `legacy/events.sh` is used in tests as the
  parity oracle for the outage algorithm.

## Installing a monitor (on every measured host)

```bash
git clone <repo> ~/netmon && cd ~/netmon/monitor
./install.sh                  # creates ~/.config/netmon/monitor.ini + systemd service
nano ~/.config/netmon/monitor.ini   # set network (network name!) and token
systemctl --user restart netmon-monitor.service
loginctl enable-linger $USER  # keep measuring without a login session / after reboot
```

Verify: `curl -H 'X-Netmon-Token: …' http://localhost:8787/api/health`

### System-wide service (LXC containers, headless servers)

Where no user session exists (e.g. a Proxmox LXC container), install the
monitor as a system service instead — it runs as a dedicated `netmon` user
with config in `/etc/netmon/monitor.ini` and data in `/var/lib/netmon/`:

```bash
apt install -y python3 iputils-ping   # everything the monitor needs
sudo ./install.sh --system
sudo nano /etc/netmon/monitor.ini && sudo systemctl restart netmon-monitor
sudo ./install.sh --system --uninstall   # removal (keeps config + data)
```

For an unprivileged LXC with Tailscale, allow `/dev/net/tun` in the
container config (standard Tailscale-in-LXC setup).

Configuration (`monitor.ini`): ping targets (`gateway=auto` = default-route
detection), intervals (ping 2 s, reach 30 s, speed hourly, heartbeat 1/min),
API port, token, local data retention (90 days). Data volume: the hourly
speed test downloads ~1.2 GB/day; on a constrained link lower `speed_bytes`
or increase `speed_interval`.

### Network connectivity (Tailscale)

The server pulls data from the monitors, so it must be able to reach them.
The intended setup is [Tailscale](https://tailscale.com): monitors and the
server share a tailnet and `monitors.toml` uses the 100.x addresses (or
MagicDNS names). On the Coolify host it's enough to run `tailscaled` —
containers on the default bridge reach the tailnet through the host's
tunnel. The `X-Netmon-Token` header is a second layer of protection.

## Deploying the evaluation server (Coolify / Docker)

1. In Coolify create an application from this repo, building from `server/`
   (Dockerfile).
2. Attach a volume at `/data` and upload `monitors.toml` into it
   (see `server/config/monitors.toml.example` — network name, Tailscale URL, token).
3. Environment variables per `.env.example` (`NETMON_TZ`, optionally `SMTP_*`).
4. Web protection: the app has no auth of its own — configure it at the
   Coolify/Traefik level (basic auth middleware) if the URL is public.

Locally: `cd server && docker compose up` → http://localhost:8000

Without Docker (development):
```bash
cd server && pip install -r requirements.txt
NETMON_DB=data/netmon.db NETMON_MONITORS=config/monitors.toml \
  uvicorn netmon_server.main:app --reload
```

### Without Docker as a systemd service (e.g. a Proxmox LXC)

The server can also run directly as a systemd service — dedicated `netmon`
user, venv inside the checkout, config in `/etc/netmon/` (`server.env`,
`monitors.toml`), database in `/var/lib/netmon-server/`:

```bash
apt install -y python3 python3-venv git
git clone <repo> /opt/netmon && cd /opt/netmon/server
sudo ./install.sh          # → web UI on :8000
sudo ./install.sh --uninstall   # removal (keeps config + data)
```

## Importing old data (from the bash version)

Historical CSVs from `log/YYYYMMDD/` on the measured hosts can be imported
into the server database (each host under its own network).

**Via the web** — set the `NETMON_IMPORT_TOKEN` environment variable (any
secret string), open `/import`, pick the network (must match the
`monitors.toml` name for live networks) and upload a `.zip` / `.tar.gz`
archive of the `log/` tree (nesting like `log-mpc/20260616/…` is fine).
Deduplication is content-based, so re-uploading the same data is safe.

**Via the CLI:**

```bash
scp -r home-host:~/netmon/log /tmp/log-home
cd server
NETMON_DB=data/netmon.db python -m netmon_server.importer \
    --network home --label "Home" /tmp/log-home
```

The import is idempotent (files are tracked by content hash; `--force`
overwrites). In Docker: `docker compose exec netmon python -m
netmon_server.importer …` (copy the log directory onto the volume).

## Daily email reports

Every day at `NETMON_REPORT_HOUR` (default 3:00, `NETMON_TZ`) the server
sends a summary of the previous day for all networks: text body + one HTML
attachment per network. Sending requires `SMTP_HOST` + `SMTP_TO`. A missed
report (server was down) is caught up after startup. Manual run / test:

```bash
python -m netmon_server.report --date 2026-07-14           # print + save HTML only
SMTP_DRYRUN=1 python -m netmon_server.report --date 2026-07-14 --send   # .eml to disk
```

## Email alerts

With SMTP configured the server also alerts as things happen (checked every
minute, one email per event — an ongoing outage alerts once, a backfilled
batch of events is grouped into a single email):

- **Outage** — a derived outage (local/internet) lasting at least
  `NETMON_ALERT_MIN_OUTAGE_S` (default 60 s).
- **Monitor unreachable** — sync has been failing for
  `NETMON_ALERT_OFFLINE_S` (default 600 s); a recovery email follows when it
  comes back. Note this only delays outage alerts: the monitor keeps
  measuring locally and events are derived after backfill.

Disable with `NETMON_ALERTS=0`.

## What is measured and how to read it

| Probe | Interval | What it tells you |
|-------|----------|-------------------|
| ping `gateway` (auto from default route) | 2 s | local link health — any loss = cable/switch/router |
| ping `quad9` (9.9.9.9) + `google` (8.8.8.8) | 2 s | internet via two independent paths |
| reach — DNS/TCP/TLS against `generate_204` | 30 s | "ping works but the internet doesn't" (DNS, dropped traffic) |
| speed — 50 MB download from Cloudflare | 1 h | speed variation (peak vs. night) |
| heartbeat | 60 s | when measuring wasn't running at all (coverage, crash vs. controlled stop) |

**Outages** are derived from pings: `local` = gateway unreachable (problem
on your side), `internet` = gateway OK but **both** public targets down in
the same round (provider problem). Losing a single public target creates no
event (noise). Loss thresholds: > 1 % problem, > 0.1 % minor loss.

**Monitor unreachable ≠ network outage**: the dashboard shows monitor
unreachability separately (sync state); actual measurement coverage is
computed from the monitor's heartbeats, which arrive complete once the
connection returns (heartbeat gap > 150 s = not measuring; `STOP` before
the gap = controlled shutdown, otherwise a crash).

## API

Monitor (`:8787`, `X-Netmon-Token` header):
`GET /api/health` · `GET /api/info` ·
`GET /api/data/{latency|reach|speed|uptime}?after_id=N&limit=5000`

Server (`:8000`): `GET /` dashboard · `GET /net/{name}?range=day|week|all&date=…`
· `GET /compare?nets=a,b` · JSON: `/api/networks`,
`/api/net/{name}/{summary|series|events}?t0=…&t1=…`, `/api/health`

## Tests

```bash
pip install pytest httpx fastapi   # + tomli on Python < 3.11
python -m pytest tests/
```

They cover outage derivation (including **parity with the original
`events.sh`** on the same fixture), import idempotency, incremental sync
with cursors and token auth, and alerting (thresholds, dedup, recovery).

## TODO / ideas

- **Upload speed measurement** — the speed test is download-only (carried
  over from the bash version). Cloudflare's `__up` endpoint could measure
  upload too; useful for ISP complaints.
- **Server DB backup** — monitors buffer only `retention_days` (90) of
  data, so the server SQLite on the Coolify volume is the only long-term
  copy. If the history matters, enable a volume backup in Coolify (or a
  `sqlite3 .backup` cron / Litestream).
