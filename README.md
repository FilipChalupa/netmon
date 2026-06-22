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
| `report.sh` | Vypíše textový souhrn z nasbíraných dat. |
| `report-html.sh` | Vygeneruje **vizuální HTML přehled s grafy** (`report.html`). |
| `events.sh` | Odvodí z pingů **čitelný seznam výpadků** (`events.csv`). |
| `reset.sh` | Vyčistí historické logy a začne čisté měření (se zálohou). |
| `latency.csv` | Log pingů (vzniká za běhu). |
| `speed.csv` | Log měření rychlosti (vzniká za běhu). |
| `reach.csv` | Log dosažitelnosti služeb — DNS / TCP / TLS (vzniká za běhu). |
| `uptime.csv` | Záznam, kdy skript běžel — „tepy" pro detekci, kdy měření/počítač neběžel (vzniká za běhu). |
| `events.csv` | Odvozený seznam výpadků (vytváří `events.sh`). |
| `archiv/` | Zálohy starých logů (vytváří `reset.sh`). |
| `netmon.run.log` | Provozní výstup procesu (pro ladění). |
| `netmon.pid` | PID běžícího procesu (vytváří `ctl.sh`). |

---

## Ovládání

Měření běží jako **systemd user služba** `netmon.service` (spustí se samo i po
rebootu, viz níže). Ovládá se přes systemd:

```bash
systemctl --user status netmon.service     # běží?
systemctl --user stop    netmon.service     # zastavit
systemctl --user start   netmon.service     # spustit
systemctl --user restart netmon.service     # po změně konfigurace v netmon.sh
```

Vyhodnocení (lze kdykoliv za běhu):

```bash
cd ~/netmon
./report.sh         # textový souhrn do terminálu (latence, ztráty, rychlost, dosažitelnost, výpadky)
./events.sh         # jen seznam výpadků → events.csv + souhrn
./report-html.sh    # vizuální HTML přehled → report.html
xdg-open report.html
```

### Vyčištění a nové měření

Když chceš začít čistě (nová série měření), použij `reset.sh`. Stará data
zazálohuje do `archiv/<datum>/`, vyprázdní logy a měření zase rozběhne:

```bash
./reset.sh            # zeptá se na potvrzení, stará data zazálohuje
./reset.sh --force    # bez ptaní (ale se zálohou)
./reset.sh --purge    # smaže úplně bez zálohy (opatrně!)
```

> Pozn.: `ctl.sh` (start/stop/status přes `nohup`) je alternativa pro ruční
> spuštění bez systemd. **Nepoužívej oboje zároveň**, ať neběží dvě instance.

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

### Dosažitelnost služeb (1×/30 s) — `reach.csv`
Ping (ICMP) routery často odsouvají stranou a leckdy „pingá", i když reálné
služby nejedou. Tahle sonda proto přes `curl` měří **čas DNS resolu, TCP
connectu a TLS handshaku** na reálný web (`google.com/generate_204`). Zachytí
výpadky typu *„ping jede, ale internet nefunguje"* — typicky padlé DNS nebo
zahozený provoz. `status=FAIL` = služba byla nedostupná.

### Rychlost (1×/h) — `speed.csv`
Stáhne ~50 MB z Cloudflare (`speed.cloudflare.com`) a změří propustnost.
Spouští se hned po startu a pak každou hodinu, takže vidíš **kolísání rychlosti
v čase** (např. večerní špička vs. noc).

### Běh měření (1×/60 s) — `uptime.csv`
Aby šlo poznat, **kdy měření vůbec neběželo** (skript zastavený, nebo dokonce
vypnutý počítač), zapisuje skript pravidelný „tep". Na startu zapíše `START`,
za běhu každou minutu `ALIVE` a při řízeném ukončení `STOP`. **Mezera mezi tepy**
delší než ~2,5 minuty znamená, že měření v tu dobu neběželo. Když je před mezerou
`STOP`, šlo o řízené zastavení; když chybí (poslední byl `ALIVE`), šlo nejspíš
o pád nebo vypnutý počítač. HTML report z toho spočítá **pokrytí měření** a vypíše
seznam přerušení.

### Výpadky (odvozené) — `events.csv`
`events.sh` projde `latency.csv` a slepí jednotlivé ztráty do **souvislých
událostí** se začátkem, koncem, délkou a rozsahem:
- `scope=local` — nedostupná brána → problém na **tvé straně** (kabel/switch/router).
- `scope=internet` — brána OK, ale **oba** veřejné cíle nedostupné → problém **u providera**.

Ideální podklad pro reklamaci: pár řádků s přesnými časy místo statisíců pingů.

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

### `reach.csv`
```
timestamp,dns_ms,tcp_ms,tls_ms,http_code,status
2026-06-16T17:00:45+02:00,4.6,1.8,39.1,204,ok
```
- `dns_ms` / `tcp_ms` / `tls_ms` = doba DNS resolu / TCP connectu / TLS handshaku v ms.
- `status` = `ok` nebo `FAIL` (služba nedostupná); u `FAIL` jsou časy prázdné.

### `uptime.csv`
```
timestamp,event
2026-06-20T10:00:00+02:00,START
2026-06-20T10:01:00+02:00,ALIVE
2026-06-20T10:02:00+02:00,STOP
```
- `event` = `START` (skript naběhl) / `ALIVE` (tep za běhu, 1×/min) / `STOP` (řízené ukončení).
- Mezera mezi dvěma řádky delší než tep = doba, kdy měření neběželo.

### `events.csv`
```
start,end,duration_s,scope,note
2026-06-16T10:00:02+02:00,2026-06-16T10:00:04+02:00,2,internet,internet (oba veřejné cíle nedostupné)
```
- `scope` = `local` (tvá strana) nebo `internet` (provider). `duration_s` = délka výpadku v sekundách.

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
Nejjednodušší je vizuální HTML přehled:
```bash
./report-html.sh && xdg-open report.html
```
Vygeneruje `report.html` se souhrnnými kartami (ztráty, latence, rychlost,
**pokrytí měření**), přehledem **běhu měření** (kdy skript/počítač neběžel),
**tabulkou výpadků** a interaktivními grafy: **latence**, **ztráta paketů**,
**dosažitelnost (DNS/TCP/TLS)** a **rychlost** v čase. Přegeneruj kdykoliv pro
aktuální data (sám si přitom přepočítá i `events.csv`). Soubor je samostatný
(grafy přes Chart.js z CDN — k zobrazení je potřeba připojení k internetu).

Případně `latency.csv` i `speed.csv` jsou běžné CSV — dají se otevřít i v
LibreOffice Calc / Excel. Pro latenci filtruj na jeden cíl (`target`), ať se
křivky nepřekrývají.

---

## Konfigurace

Hodnoty se mění nahoře v `netmon.sh` (po změně udělej `./ctl.sh stop && ./ctl.sh start`):

| Proměnná | Výchozí | Význam |
|----------|---------|--------|
| `PING_INTERVAL` | 2 | sekund mezi koly pingů |
| `PING_TIMEOUT` | 2 | sekund čekání na odpověď |
| `REACH_INTERVAL` | 30 | sekund mezi reach sondami (DNS/TCP/TLS) |
| `REACH_URL` | google/generate_204 | cíl reach sondy (vrací 204, bez těla) |
| `SPEED_INTERVAL` | 3600 | sekund mezi testy rychlosti (3600 = 1×/h) |
| `SPEED_BYTES` | 50000000 | bajtů na test rychlosti (50 MB) |
| `HEARTBEAT_INTERVAL` | 60 | sekund mezi „tepy" do `uptime.csv` (záznam o běhu) |
| `TARGETS` | gateway/quad9/google | cíle pingu ve tvaru `"popisek=IP"` |

**Objem dat:** hodinový test stahuje 50 MB ≈ **1,2 GB/den**. Na měřené lince
sniž `SPEED_BYTES` nebo prodluž `SPEED_INTERVAL`.

---

## Poznámky a omezení

- **Autostart po rebootu je zapnutý** přes systemd user službu `netmon.service`
  + `loginctl enable-linger`, takže měření jede i po restartu bez přihlášení.
  Vypnout autostart: `systemctl --user disable --now netmon.service`.
- Některé veřejné IP (např. `1.1.1.1`) v této síti **neodpovídají na ICMP** —
  proto je v cílech Quad9 (9.9.9.9) místo Cloudflare. Když přidáváš vlastní cíl,
  ověř, že na ping reaguje, jinak budeš mít falešných 100 % ztrát.
- Měří se přes rozhraní `eno1` (drát). Při testu odpoj/nepoužívej WiFi, ať data
  nejsou zkreslená druhým spojem.
