#!/usr/bin/env bash
#
# netmon.sh — měření kvality připojení (výpadky, latence, jitter, rychlost)
# Píše dva CSV logy do adresáře skriptu. Spouštěj přes ./ctl.sh start
#
set -u

# ---- Konfigurace (klidně uprav) ------------------------------------------
PING_INTERVAL=2          # sekund mezi koly pingů
PING_TIMEOUT=2           # sekund čekání na odpověď pingu
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

# Hlavičky (jen pokud soubor ještě neexistuje)
[ -f "$LAT_LOG" ] || echo "timestamp,target,ip,status,rtt_ms" >> "$LAT_LOG"
[ -f "$SPD_LOG" ] || echo "timestamp,down_mbps,bytes,seconds,http_code" >> "$SPD_LOG"

running=1
trap 'running=0' TERM INT

now_iso() { date --iso-8601=seconds; }

probe_target() {
  local name="$1" ip="$2" out rtt
  out=$(ping -n -c1 -W "$PING_TIMEOUT" "$ip" 2>/dev/null)
  if [ $? -eq 0 ]; then
    rtt=$(printf '%s\n' "$out" | sed -n 's/.*time=\([0-9.]*\) ms.*/\1/p')
    echo "$(now_iso),$name,$ip,ok,${rtt:-}" >> "$LAT_LOG"
  else
    echo "$(now_iso),$name,$ip,LOSS," >> "$LAT_LOG"
  fi
}

speed_test() {
  local res code bps secs bytes mbps
  # -w: rychlost v bajtech/s, velikost, čas, http kód
  res=$(curl -s -o /dev/null \
        -w '%{speed_download} %{size_download} %{time_total} %{http_code}' \
        --max-time 120 "$SPEED_URL" 2>/dev/null)
  read -r bps bytes secs code <<<"$res"
  if [ "${code:-000}" = "200" ] && [ -n "${bps:-}" ]; then
    mbps=$(awk -v b="$bps" 'BEGIN{printf "%.2f", b*8/1000000}')
    echo "$(now_iso),$mbps,$bytes,$secs,$code" >> "$SPD_LOG"
  else
    echo "$(now_iso),,,${secs:-},${code:-FAIL}" >> "$SPD_LOG"
  fi
}

# Hned na startu jeden test rychlosti, ať máš referenci
last_speed=0
speed_test
last_speed=$(date +%s)

while [ "$running" -eq 1 ]; do
  for t in "${TARGETS[@]}"; do
    probe_target "${t%%=*}" "${t#*=}"
  done

  nowsec=$(date +%s)
  if [ $(( nowsec - last_speed )) -ge "$SPEED_INTERVAL" ]; then
    speed_test
    last_speed=$(date +%s)
  fi

  sleep "$PING_INTERVAL"
done

echo "$(now_iso),--,--,STOPPED," >> "$LAT_LOG"
