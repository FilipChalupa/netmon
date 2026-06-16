# netmon — měření kvality připojení

Jednoduchá sada skriptů, která pár dní sbírá statistiky o kvalitě drátového
připojení: **výpadky, latenci, jitter a kolísavou rychlost**. Nepotřebuje nic
instalovat (jen `ping`, `curl`, `bash`) a běží na pozadí.

---

## Obsah složky

| Soubor | K čemu je |
|--------|-----------|
| `netmon.sh` | Vlastní měřicí smyčka (běží na pozadí). Konfigurace je nahoře v souboru. |
| `ctl.sh` | Ovládání: `start` / `stop` / `status`. |
| `report.sh` | Vypíše souhrn z nasbíraných dat. |
| `latency.csv` | Log pingů (vzniká za běhu). |
| `speed.csv` | Log měření rychlosti (vzniká za běhu). |
| `netmon.run.log` | Provozní výstup procesu (pro ladění). |
| `netmon.pid` | PID běžícího procesu (vytváří `ctl.sh`). |

---

## Ovládání

```bash
cd ~/netmon
./ctl.sh start      # spustí měření na pozadí (přežije zavření terminálu)
./ctl.sh status     # běží? kolik je nasbíráno záznamů
./ctl.sh stop       # ukončí měření
./report.sh         # souhrnný report z nasbíraných dat (lze kdykoliv za běhu)
```

---

## Co se měří a proč

### Ping (každé 2 s) — `latency.csv`
Pinguje tři cíle, aby šlo **rozlišit, kde problém vzniká**:

| Cíl | Adresa | Co znamená výpadek |
|-----|--------|--------------------|
| `gateway` | 10.0.0.1 | Lokální síť (kabel, switch, port routeru) |
| `quad9` | 9.9.9.9 | Internet / cesta k providerovi |
| `google` | 8.8.8.8 | Internet / druhý nezávislý cíl |

**Jak číst příčinu výpadku:**
- Vypadne **gateway** → problém je u tebe doma (kabel, switch, port, NIC).
- Gateway jede, ale vypadnou **quad9 i google současně** → problém je u providera / na cestě ven.
- Vypadne jen **jeden** z internetových cílů → spíš problém daného cíle nebo cesty k němu, ne tvého připojení.

### Rychlost (1×/h) — `speed.csv`
Stáhne ~50 MB z Cloudflare (`speed.cloudflare.com`) a změří propustnost.
Spouští se hned po startu a pak každou hodinu, takže vidíš **kolísání rychlosti
v čase** (např. večerní špička vs. noc).

---

## Formát logů

### `latency.csv`
```
timestamp,target,ip,status,rtt_ms
2026-06-16T15:29:37+02:00,google,8.8.8.8,ok,2.18
2026-06-16T15:30:11+02:00,google,8.8.8.8,LOSS,
```
- `status` = `ok` (odpověď přišla) nebo `LOSS` (paket ztracen / timeout).
- `rtt_ms` = odezva v milisekundách (u `LOSS` prázdné).
- Každé kolo = jeden řádek na cíl, takže tři řádky se stejným časem patří k sobě.

### `speed.csv`
```
timestamp,down_mbps,bytes,seconds,http_code
2026-06-16T15:24:09+02:00,136.57,50000000,2.928853,200
```
- `down_mbps` = rychlost stahování v **megabitech/s** (prázdné = test selhal).
- `http_code` = `200` při úspěchu, jinak `FAIL` nebo HTTP kód chyby.

---

## Vyhodnocení

### Rychlý souhrn
```bash
./report.sh
```
Vypíše pro každý cíl počet vzorků, **% ztracených paketů**, průměrnou / min / max
latenci, nejdelší souvislé výpadky a statistiku rychlosti.

### Na co se při vyhodnocení dívat

| Metrika | Zdravé (drát) | Podezřelé |
|---------|---------------|-----------|
| Ztráta paketů (internet) | < 0,1 % | > 1 % = znatelné problémy |
| Ztráta na gateway | 0 % | jakákoliv = problém lokální linky/kabelu |
| Latence ke gateway | < 1 ms | desítky ms = přetížený/vadný lokální HW |
| Latence na internet | jednotky až nízké desítky ms | velký rozptyl (jitter) = problém s linkou |
| Jitter (max − min) | malý a stabilní | velké výkyvy škodí hovorům/hrám/videu |
| Rychlost | blízko tarifu, stabilní | propady jen ve špičce = přetížení u providera |

### Užitečné dotazy nad daty (ad‑hoc, bez reportu)

Počet a procento ztrát na Google:
```bash
awk -F, '$2=="google"{t++; if($4=="LOSS")l++} END{printf "%d/%d ztrát (%.2f%%)\n",l,t,l/t*100}' latency.csv
```

Všechny výpadky internetu (kdy vypadly quad9 i google ve stejném kole):
```bash
awk -F, '$4=="LOSS" && ($2=="quad9"||$2=="google"){c[$1]++} END{for(t in c) if(c[t]>=2) print t}' latency.csv | sort
```

Vývoj rychlosti podle hodiny (průměr):
```bash
awk -F, 'NR>1 && $2!=""{h=substr($1,1,13); s[h]+=$2; n[h]++} END{for(h in s) printf "%s  %.1f Mbit/s\n",h,s[h]/n[h]}' speed.csv | sort
```

Špičky latence nad 50 ms na internet:
```bash
awk -F, '$2=="google" && $5+0>50 {print $1, $5" ms"}' latency.csv
```

### Grafy
`latency.csv` i `speed.csv` jsou běžné CSV — dají se otevřít v LibreOffice
Calc / Excel a vykreslit (latence v čase, rychlost v čase). Pro latenci doporučuji
filtrovat na jeden cíl (`target`), ať se křivky nepřekrývají.

---

## Konfigurace

Hodnoty se mění nahoře v `netmon.sh` (po změně udělej `./ctl.sh stop && ./ctl.sh start`):

| Proměnná | Výchozí | Význam |
|----------|---------|--------|
| `PING_INTERVAL` | 2 | sekund mezi koly pingů |
| `PING_TIMEOUT` | 2 | sekund čekání na odpověď |
| `SPEED_INTERVAL` | 3600 | sekund mezi testy rychlosti (3600 = 1×/h) |
| `SPEED_BYTES` | 50000000 | bajtů na test rychlosti (50 MB) |
| `TARGETS` | gateway/quad9/google | cíle pingu ve tvaru `"popisek=IP"` |

**Objem dat:** hodinový test stahuje 50 MB ≈ **1,2 GB/den**. Na měřené lince
sniž `SPEED_BYTES` nebo prodluž `SPEED_INTERVAL`.

---

## Poznámky a omezení

- **Po rebootu se měření samo nespustí.** Pokud počítač během sběru restartuješ,
  spusť znovu `./ctl.sh start` (nebo si nech dodělat autostart přes systemd / `@reboot` cron).
- Některé veřejné IP (např. `1.1.1.1`) v této síti **neodpovídají na ICMP** —
  proto je v cílech Quad9 (9.9.9.9) místo Cloudflare. Když přidáváš vlastní cíl,
  ověř, že na ping reaguje, jinak budeš mít falešných 100 % ztrát.
- Měří se přes rozhraní `eno1` (drát). Při testu odpoj/nepoužívej WiFi, ať data
  nejsou zkreslená druhým spojem.
