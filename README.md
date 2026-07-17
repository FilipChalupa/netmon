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

## Quick start: single binary (all-in-one)

Download the binary for your OS from
[Releases](../../releases) (Linux x86_64, macOS arm64/x86_64, Windows) and run:

```bash
./netmon --network home        # measure this machine's network + web UI on :8000
```

One process runs the measuring monitor *and* the results web server —
open http://localhost:8000. Data lives in `~/.local/share/netmon/`
(`--data-dir` to change). The monitor's pull API stays on :8787, so a
central evaluation server can still adopt this instance later. Subcommands
`netmon monitor` / `netmon server` run just one half; a `monitor.ini`
(see below) is honored when present.

Binaries are built by the release workflow on every `v*` tag.

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

Notes for (unprivileged) LXC containers:

- **Clone outside `/root` and `/home`** (e.g. `/opt/netmon`) — the service
  runs as the `netmon` user, which cannot traverse private home directories.
  The installer refuses such paths with a hint.
- **ICMP for the service user**: in an unprivileged LXC, `ping` works for
  root but not for the `netmon` user (file capabilities don't apply), which
  shows up as 100 % packet loss while speed/reach work fine. Fix inside the
  container — note the upper bound must stay within the container's mapped
  GIDs, `2147483647` is rejected with *Invalid argument*:
  ```bash
  echo 'net.ipv4.ping_group_range = 0 65534' > /etc/sysctl.d/99-ping.conf
  sysctl --system && systemctl restart netmon-monitor
  ```
- For Tailscale in an unprivileged LXC, allow `/dev/net/tun` in the
  container config (standard Tailscale-in-LXC setup).

Configuration (`monitor.ini`): ping targets (`gateway=auto` = default-route
detection), intervals (ping 2 s, reach 30 s, speed hourly, heartbeat 1/min),
API port, token, local data retention (90 days). Data volume: the hourly
speed test downloads ~1.2 GB/day; on a constrained link lower `speed_bytes`
or increase `speed_interval`. On fast lines a test finishing under
`speed_min_seconds` (3 s) is automatically re-measured once with a larger
payload (up to `speed_max_bytes`, 200 MB) so TCP ramp-up doesn't
underestimate the result.

### Network connectivity (Tailscale)

The server pulls data from the monitors, so it must be able to reach them.
The intended setup is [Tailscale](https://tailscale.com): monitors and the
server share a tailnet and `monitors.toml` uses the 100.x addresses (or
MagicDNS names). On the Coolify host it's enough to run `tailscaled` —
containers on the default bridge reach the tailnet through the host's
tunnel. The `X-Netmon-Token` header is a second layer of protection.
A monitor on the **same LAN** as the server needs no Tailscale at all —
just use its LAN IP in `monitors.toml`.

Alternative without Tailscale on the server: if some other host on the LAN
already runs Tailscale, route the whole LAN through it — a static route on
the router (`100.64.0.0/10 → <tailscale host>`) plus masquerade on that
host (`iptables -t nat -A POSTROUTING -s <LAN subnet> -o tailscale0 -j
MASQUERADE`, persisted). Do **not** use `--exit-node` or `--accept-routes`
on LAN clients — accepting a route to your own subnet creates a loop.

## Deploying the evaluation server (Coolify / Docker)

1. In Coolify create an application from this repo: Build Pack **Dockerfile**,
   Base Directory **`/server`**, Ports Exposes **8000**.
2. Storages: a **volume mount** at `/data` (SQLite database) and a **file
   mount** at `/data/monitors.toml` with the monitor list
   (see `server/config/monitors.toml.example` — network name, URL, token;
   watch out for stray whitespace in `name`, it must match `monitor.ini`
   exactly).
3. Environment variables per `.env.example` (`NETMON_TZ`, optionally `SMTP_*`).
4. Web protection: the app has no auth of its own — protect it at the proxy
   level (Traefik basic auth / Cloudflare Access) if the URL is public.

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
into the server database (each host under its own network — `--network`
must match the `monitors.toml` name so history merges with live data):

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
- **Reach failure** — `NETMON_ALERT_REACH_FAILS` consecutive reach probe
  failures (default 10 ≈ 5 min): "pings work but the internet doesn't"
  (broken DNS, filtered traffic). Suppressed while a ping-derived outage
  overlaps, so a hard outage sends a single email.
- **Speed degradation** — the median of the recent tests (6 h) drops below
  `NETMON_ALERT_SPEED_PCT` % (default 50, 0 disables) of the 30-day
  baseline median; a recovery email follows once it's back.
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
| public IP (api.ipify.org, stored only on change) | 15 min | ISP identification via rDNS; IP changes often coincide with outages |

**Outages** are derived from pings: `local` = gateway unreachable (problem
on your side), `internet` = gateway OK but **both** public targets down in
the same round (provider problem). Losing a single public target creates no
event (noise). Loss thresholds: > 1 % problem, > 0.1 % minor loss.

**Monitor unreachable ≠ network outage**: the dashboard shows monitor
unreachability separately (sync state); actual measurement coverage is
computed from the monitor's heartbeats, which arrive complete once the
connection returns (heartbeat gap > 150 s = not measuring; `STOP` before
the gap = controlled shutdown, otherwise a crash).

**Notes**: the network page has a notes panel — text + time, scoped to
selected networks or general (none selected = applies to all). Notes show
as dashed markers with hover tooltips in all charts (network detail and
comparison), are listed in the daily email report, and clicking a chart
prefills the note form with the clicked moment. Derived outages are also
shaded directly in the charts (red = local, amber = internet), and a
GitHub-style calendar heatmap at the bottom of the network page shows a
year of daily internet loss — click a day to open it.

## API

Monitor (`:8787`, `X-Netmon-Token` header):
`GET /api/health` · `GET /api/info` ·
`GET /api/data/{latency|reach|speed|uptime}?after_id=N&limit=5000`

Server (`:8000`): `GET /` dashboard ·
`GET /net/{name}?range=day|week|all&date=…` (or `range=custom&from=…&to=…`)
· `GET /compare?nets=a,b` · `GET /help` (how the probes measure) · JSON: `/api/networks`,
`/api/net/{name}/{summary|series|events}?t0=…&t1=…`,
`/api/net/{name}/heatmap?days=365`, `/api/health`, notes:
`GET /api/notes?t0=…&t1=…&nets=a,b` · `POST /api/notes` · `DELETE /api/notes/{id}`

### MCP (LLM clients)

The server exposes an MCP endpoint at `/mcp` (streamable HTTP) so Claude Code
or Claude Desktop can query the monitoring data directly:

```bash
claude mcp add --transport http netmon http://<server>:8000/mcp
```

Tools: `list_networks`, `get_summary`, `get_speed_history`,
`get_daily_heatmap`, `get_notes`, `add_note` (the only write operation).
The endpoint shares the web UI's trust boundary — keep it on a trusted
network (tailnet).

## Tests

```bash
pip install pytest httpx fastapi   # + tomli on Python < 3.11
python -m pytest tests/
```

They cover outage derivation (including **parity with the original
`events.sh`** on the same fixture), import idempotency, incremental sync
with cursors and token auth, alerting (thresholds, dedup, recovery),
notes (scoping, report inclusion, schema migration) and the heatmap's
per-local-day aggregation.

## TODO / ideas

- **Upload speed measurement** — the speed test is download-only (carried
  over from the bash version). Cloudflare's `__up` endpoint could measure
  upload too; useful for ISP complaints.
- **Server DB backup** — monitors buffer only `retention_days` (90) of
  data, so the server SQLite on the Coolify volume is the only long-term
  copy. If the history matters, enable a volume backup in Coolify (or a
  `sqlite3 .backup` cron / Litestream).
- **Long-term DB growth** — latency grows by roughly 400k rows/day for
  three networks (a few GB per year). Charts stay fast thanks to SQL
  bucketing over indexes, but eventually raw pings older than ~90 days
  could be downsampled into per-minute aggregates (avg rtt, loss count per
  target) and dropped, keeping derived events intact.
