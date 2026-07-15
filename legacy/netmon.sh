#!/usr/bin/env bash
#
# netmon.sh — měření kvality připojení (výpadky, latence, jitter, rychlost, dosažitelnost)
# Píše CSV logy do log/RRRRMMDD/ (jeden podadresář na den). Ovládej přes systemd
# (netmon.service) nebo ./ctl.sh
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
HEARTBEAT_INTERVAL=60    # sekund mezi „tepy" do uptime.csv (záznam, že skript běží)

# Brána se detekuje automaticky z výchozí trasy (přežije změnu sítě).
# Když detekce selže, použije se fallback níže.
GATEWAY_IP="$(ip route show default 2>/dev/null | awk '{print $3; exit}')"
GATEWAY_IP="${GATEWAY_IP:-10.30.0.1}"

# Cíle pingu: "popisek=IP". Brána = zdraví lokální linky; zbytek = internet/ISP.
TARGETS=(
  "gateway=${GATEWAY_IP}"
  "quad9=9.9.9.9"
  "google=8.8.8.8"
)
# --------------------------------------------------------------------------

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_ROOT="$DIR/log"   # logy se píší do log/RRRRMMDD/ (jeden podadresář na den)

# Přepne zápis na adresář dnešního dne (log/RRRRMMDD). Při prvním použití dne ho
# založí i s hlavičkami CSV. Volá se před každým kolem, takže se logy po půlnoci
# samy „překlopí" do nového dne.
LAT_LOG=""; SPD_LOG=""; RCH_LOG=""; UPT_LOG=""; LOG_DAY=""
rotate_logs() {
  local day; day="$(date +%Y%m%d)"
  [ "$day" = "$LOG_DAY" ] && return        # stejný den → není co dělat
  local d="$LOG_ROOT/$day"
  mkdir -p "$d"
  LAT_LOG="$d/latency.csv"; SPD_LOG="$d/speed.csv"
  RCH_LOG="$d/reach.csv";   UPT_LOG="$d/uptime.csv"
  [ -f "$LAT_LOG" ] || echo "timestamp,target,ip,status,rtt_ms" >> "$LAT_LOG"
  [ -f "$SPD_LOG" ] || echo "timestamp,down_mbps,bytes,seconds,http_code" >> "$SPD_LOG"
  [ -f "$RCH_LOG" ] || echo "timestamp,dns_ms,tcp_ms,tls_ms,http_code,status" >> "$RCH_LOG"
  [ -f "$UPT_LOG" ] || echo "timestamp,event" >> "$UPT_LOG"
  LOG_DAY="$day"
}

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

# Záznam, že skript běží: START hned na startu, pak pravidelný „tep" (ALIVE)
# a STOP při ukončení. Mezera mezi tepy v uptime.csv = skript/počítač neběžel.
rotate_logs
echo "$(now_iso),START" >> "$UPT_LOG"
last_beat=$(date +%s)

# Hned na startu jeden test rychlosti + reach, ať máš referenci
last_speed=0; last_reach=0
speed_test "$(now_iso)"; last_speed=$(date +%s)
reach_probe "$(now_iso)"; last_reach=$(date +%s)

while [ "$running" -eq 1 ]; do
  rotate_logs                        # po půlnoci přepne na nový den
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
  if [ $(( nowsec - last_beat )) -ge "$HEARTBEAT_INTERVAL" ]; then
    echo "$(now_iso),ALIVE" >> "$UPT_LOG"; last_beat=$(date +%s)
  fi

  sleep "$PING_INTERVAL"
done

echo "$(now_iso),STOP" >> "$UPT_LOG"
echo "$(now_iso),--,--,STOPPED," >> "$LAT_LOG"
