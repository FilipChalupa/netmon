#!/usr/bin/env bash
#
# netmon.sh — měření kvality připojení (výpadky, latence, jitter, rychlost, dosažitelnost)
# Píše CSV logy do adresáře skriptu. Ovládej přes systemd (netmon.service) nebo ./ctl.sh
#
set -u

# ---- Konfigurace (klidně uprav) ------------------------------------------
PING_INTERVAL=2          # sekund mezi koly pingů
PING_TIMEOUT=2           # sekund čekání na odpověď pingu
REACH_INTERVAL=30        # sekund mezi reachability sondami (DNS/TCP/TLS)
REACH_URL="https://www.google.com/generate_204"   # cíl reach sondy (vrací 204, bez těla)
SPEED_INTERVAL=3600      # sekund mezi měřeními rychlosti (3600 = 1×/h)
SPEED_BYTES=50000000     # kolik bajtů stáhnout pro test rychlosti (50 MB)
SPEED_URL="https://speed.cloudflare.com/__down?bytes=${SPEED_BYTES}"

# Cíle pingu: "popisek=IP". Brána = zdraví lokální linky; zbytek = internet/ISP.
TARGETS=(
  "gateway=10.0.0.1"
  "quad9=9.9.9.9"
  "google=8.8.8.8"
)
# --------------------------------------------------------------------------

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAT_LOG="$DIR/latency.csv"
SPD_LOG="$DIR/speed.csv"
RCH_LOG="$DIR/reach.csv"

# Hlavičky (jen pokud soubor ještě neexistuje)
[ -f "$LAT_LOG" ] || echo "timestamp,target,ip,status,rtt_ms" >> "$LAT_LOG"
[ -f "$SPD_LOG" ] || echo "timestamp,down_mbps,bytes,seconds,http_code" >> "$SPD_LOG"
[ -f "$RCH_LOG" ] || echo "timestamp,dns_ms,tcp_ms,tls_ms,http_code,status" >> "$RCH_LOG"

running=1
trap 'running=0' TERM INT

now_iso() { date --iso-8601=seconds; }

# Pingne jeden cíl; všechny cíle v jednom kole sdílejí stejný timestamp $1
probe_target() {
  local ts="$1" name="$2" ip="$3" out rtt
  out=$(ping -n -c1 -W "$PING_TIMEOUT" "$ip" 2>/dev/null)
  if [ $? -eq 0 ]; then
    rtt=$(printf '%s\n' "$out" | sed -n 's/.*time=\([0-9.]*\) ms.*/\1/p')
    echo "$ts,$name,$ip,ok,${rtt:-}" >> "$LAT_LOG"
  else
    echo "$ts,$name,$ip,LOSS," >> "$LAT_LOG"
  fi
}

# Reachability: čas DNS resolu, TCP connectu a TLS handshaku (přes curl)
reach_probe() {
  local ts="$1" res nl con app code dns tcp tls
  res=$(curl -sS -o /dev/null --max-time 10 \
        -w '%{time_namelookup} %{time_connect} %{time_appconnect} %{http_code}' \
        "$REACH_URL" 2>/dev/null)
  read -r nl con app code <<<"$res"
  if [ -n "${code:-}" ] && [ "${code:-000}" != "000" ]; then
    dns=$(awk -v x="${nl:-0}" 'BEGIN{printf "%.1f", x*1000}')
    tcp=$(awk -v a="${con:-0}" -v b="${nl:-0}" 'BEGIN{d=(a-b)*1000; printf "%.1f", d<0?0:d}')
    tls=$(awk -v a="${app:-0}" -v b="${con:-0}" 'BEGIN{d=(a-b)*1000; printf "%.1f", d<0?0:d}')
    echo "$ts,$dns,$tcp,$tls,$code,ok" >> "$RCH_LOG"
  else
    echo "$ts,,,,${code:-000},FAIL" >> "$RCH_LOG"
  fi
}

speed_test() {
  local ts="$1" res code bps secs bytes mbps
  res=$(curl -s -o /dev/null \
        -w '%{speed_download} %{size_download} %{time_total} %{http_code}' \
        --max-time 120 "$SPEED_URL" 2>/dev/null)
  read -r bps bytes secs code <<<"$res"
  if [ "${code:-000}" = "200" ] && [ -n "${bps:-}" ]; then
    mbps=$(awk -v b="$bps" 'BEGIN{printf "%.2f", b*8/1000000}')
    echo "$ts,$mbps,$bytes,$secs,$code" >> "$SPD_LOG"
  else
    echo "$ts,,,${secs:-},${code:-FAIL}" >> "$SPD_LOG"
  fi
}

# Hned na startu jeden test rychlosti + reach, ať máš referenci
last_speed=0; last_reach=0
speed_test "$(now_iso)"; last_speed=$(date +%s)
reach_probe "$(now_iso)"; last_reach=$(date +%s)

while [ "$running" -eq 1 ]; do
  ts=$(now_iso)                      # jeden timestamp pro celé kolo
  for t in "${TARGETS[@]}"; do
    probe_target "$ts" "${t%%=*}" "${t#*=}"
  done

  nowsec=$(date +%s)
  if [ $(( nowsec - last_reach )) -ge "$REACH_INTERVAL" ]; then
    reach_probe "$(now_iso)"; last_reach=$(date +%s)
  fi
  if [ $(( nowsec - last_speed )) -ge "$SPEED_INTERVAL" ]; then
    speed_test "$(now_iso)"; last_speed=$(date +%s)
  fi

  sleep "$PING_INTERVAL"
done

echo "$(now_iso),--,--,STOPPED," >> "$LAT_LOG"
