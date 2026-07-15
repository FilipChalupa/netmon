# netmon 2 — měření kvality připojení ve více sítích

Dvoudílný systém v Pythonu pro dlouhodobé měření kvality připojení
(**výpadky, latence, jitter, rychlost, dosažitelnost služeb**) v několika
sítích najednou, s centrálním vyhodnocením a webovými grafy.

```
┌─ síť A (doma) ──────────┐        ┌─ kdekoliv na internetu (Coolify) ─────┐
│ monitor (Python stdlib) │◄───────┤ evaluation server (FastAPI + SQLite)  │
│ ping/reach/speed → SQLite  pull  │  · průběžný sync z monitorů           │
│ mini HTTP API :8787     │  přes  │  · web: dashboard, detail, porovnání  │
└─────────────────────────┘  Tail- │  · odvození výpadků, pokrytí měření   │
┌─ síť B (chata) ─────────┐  scale │  · denní e-mailové reporty            │
│ monitor                 │◄───────┤                                       │
└─────────────────────────┘        └───────────────────────────────────────┘
```

- **`monitor/`** — měřicí agent. Čistý Python **bez závislostí** (stačí
  `python3`, `ping`, na Ubuntu nic neinstaluješ). Měří, ukládá lokálně do
  SQLite a vystavuje mini HTTP API, ze kterého si server data stahuje.
  Monitor data bufferuje — když server neběží nebo síť vypadne, po obnovení
  se vše doplní.
- **`server/`** — evaluation server. Stahuje data ze všech monitorů
  (inkrementálně, idempotentně), ukládá do jedné SQLite databáze a zobrazuje
  webový frontend (česky, tmavé téma): dashboard sítí, detail s grafy
  latence / ztrát / DNS-TCP-TLS / rychlosti, tabulka výpadků, pokrytí měření
  a **porovnání sítí přes sebe**. Posílá denní e-mailové reporty.
- **`legacy/`** — původní bashová verze (jeden stroj, CSV + HTML report).
  Slouží už jen jako reference; `legacy/events.sh` se používá v testech jako
  paritní orákulum algoritmu výpadků.

## Instalace monitoru (na každém měřeném stroji)

```bash
git clone <repo> ~/netmon && cd ~/netmon/monitor
./install.sh                  # vytvoří ~/.config/netmon/monitor.ini + systemd službu
nano ~/.config/netmon/monitor.ini   # nastav network (jméno sítě!) a token
systemctl --user restart netmon-monitor.service
loginctl enable-linger $USER  # ať měření běží i bez přihlášení / po rebootu
```

Ověření: `curl -H 'X-Netmon-Token: …' http://localhost:8787/api/health`

Konfigurace (`monitor.ini`): cíle pingu (`gateway=auto` = autodetekce brány),
intervaly (ping 2 s, reach 30 s, speed 1×/h, tep 1×/min), port API, token,
retence lokálních dat (90 dnů). Objem dat: hodinový speed test = ~1,2 GB/den;
na slabé lince sniž `speed_bytes` nebo prodluž `speed_interval`.

### Síťové propojení (Tailscale)

Server stahuje data z monitorů — potřebuje se na ně dostat. Počítá se
s [Tailscale](https://tailscale.com): monitory i server jsou ve stejném
tailnetu a v `monitors.toml` se použijí 100.x adresy (nebo MagicDNS jména).
Na Coolify hostu stačí spuštěný `tailscaled` — kontejnery se přes výchozí
bridge dostanou na tailnet skrz tunel hosta. Token v hlavičce
`X-Netmon-Token` je druhá vrstva ochrany.

## Nasazení evaluation serveru (Coolify / Docker)

1. V Coolify vytvoř aplikaci z tohoto repa, build z `server/` (Dockerfile).
2. Připoj volume na `/data` a nahraj do něj `monitors.toml`
   (viz `server/config/monitors.toml.example` — jméno sítě, Tailscale URL, token).
3. Env proměnné podle `.env.example` (`NETMON_TZ`, volitelně `SMTP_*`).
4. Ochrana webu: aplikace sama auth nemá — nastav ji na úrovni
   Coolify/Traefik (basic auth middleware), pokud má být URL veřejná.

Lokálně: `cd server && docker compose up` → http://localhost:8000

Bez Dockeru (vývoj):
```bash
cd server && pip install -r requirements.txt
NETMON_DB=data/netmon.db NETMON_MONITORS=config/monitors.toml \
  uvicorn netmon_server.main:app --reload
```

## Import starých dat (z bashové verze)

Historická CSV z `log/RRRRMMDD/` na měřených strojích jdou naimportovat
do serverové databáze (každý stroj pod svou síť):

```bash
scp -r stroj-doma:~/netmon/log /tmp/log-doma
cd server
NETMON_DB=data/netmon.db python -m netmon_server.importer \
    --network doma --label "Doma" /tmp/log-doma
```

Import je idempotentní (soubory se evidují podle obsahu; `--force` přepíše).
V Dockeru: `docker compose exec netmon python -m netmon_server.importer …`
(log adresář nakopíruj na volume).

## Denní e-mailové reporty

Server každý den v `NETMON_REPORT_HOUR` (výchozí 3:00, `NETMON_TZ`) pošle
souhrn za předchozí den za všechny sítě: textové tělo + HTML příloha per síť.
Posílá se jen s vyplněným `SMTP_HOST` + `SMTP_TO`. Zmeškaný report (server
neběžel) se dožene po startu. Ruční spuštění / test:

```bash
python -m netmon_server.report --date 2026-07-14           # jen vypíše + uloží HTML
SMTP_DRYRUN=1 python -m netmon_server.report --date 2026-07-14 --send   # .eml na disk
```

## Co se měří a jak se to čte

| Sonda | Interval | Co říká |
|-------|----------|---------|
| ping `gateway` (auto z výchozí trasy) | 2 s | zdraví lokální linky — jakákoliv ztráta = kabel/switch/router |
| ping `quad9` (9.9.9.9) + `google` (8.8.8.8) | 2 s | internet dvěma nezávislými cestami |
| reach — DNS/TCP/TLS na `generate_204` | 30 s | „ping jede, ale internet nefunguje" (DNS, zahozený provoz) |
| speed — stažení 50 MB z Cloudflare | 1 h | kolísání rychlosti (špička vs. noc) |
| heartbeat | 60 s | kdy měření vůbec neběželo (pokrytí, pády vs. řízená zastavení) |

**Výpadky** se odvozují z pingů: `local` = brána nedostupná (problém u tebe),
`internet` = brána OK, ale **oba** veřejné cíle v tomtéž kole nedostupné
(problém u providera). Ztráta jediného veřejného cíle událost netvoří (šum).
Prahy ztrát: > 1 % problém, > 0,1 % drobné ztráty.

**Monitor nedostupný ≠ výpadek sítě**: dashboard ukazuje nedostupnost
monitoru zvlášť (stav syncu); skutečné pokrytí měření se počítá z tepů
monitoru, které dorazí kompletní po obnovení spojení (mezera > 150 s mezi
tepy = měření neběželo; `STOP` před mezerou = řízené zastavení, jinak pád).

## API

Monitor (`:8787`, hlavička `X-Netmon-Token`):
`GET /api/health` · `GET /api/info` ·
`GET /api/data/{latency|reach|speed|uptime}?after_id=N&limit=5000`

Server (`:8000`): `GET /` dashboard · `GET /net/{name}?range=day|week|all&date=…`
· `GET /compare?nets=a,b` · JSON: `/api/networks`,
`/api/net/{name}/{summary|series|events}?t0=…&t1=…`, `/api/health`

## Testy

```bash
pip install pytest httpx fastapi   # + tomli na Pythonu < 3.11
python -m pytest tests/
```

Pokrývají odvození výpadků (včetně **parity s původním `events.sh`** nad
stejnou fixture), idempotenci importu a inkrementální sync s kurzory a tokenem.
