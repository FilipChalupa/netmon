#!/usr/bin/env bash
# reset.sh — vyčistí historické záznamy a začne čisté měření.
# Stará data zazálohuje do podsložky archiv/ (nepřijdeš o ně), pak vyprázdní logy.
# Použití:
#   ./reset.sh            # zeptá se na potvrzení
#   ./reset.sh --force    # bez ptaní
#   ./reset.sh --purge    # smaže úplně (bez zálohy) — opatrně!
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS=(latency.csv speed.csv reach.csv uptime.csv events.csv netmon.run.log)

MODE="ask"
case "${1:-}" in
  --force) MODE="force";;
  --purge) MODE="purge";;
  "") MODE="ask";;
  *) echo "Neznámý přepínač: $1"; exit 1;;
esac

# Běží služba / proces?
svc_active=0
systemctl --user is-active --quiet netmon.service 2>/dev/null && svc_active=1

if [ "$MODE" = "ask" ]; then
  echo "Vyčistí tyto logy ve $DIR:"
  for f in "${LOGS[@]}"; do [ -f "$DIR/$f" ] && echo "  - $f ($(wc -l <"$DIR/$f") řádků)"; done
  [ "$svc_active" = 1 ] && echo "Služba netmon.service teď BĚŽÍ — bude na chvíli zastavena."
  read -r -p "Pokračovat? Stará data se zazálohují do archiv/ [a/N] " ans
  case "$ans" in a|A|y|Y|ano|Ano) ;; *) echo "Zrušeno."; exit 0;; esac
fi

# Zastavit měření, ať se do logů nepíše během mazání
restart=0
if [ "$svc_active" = 1 ]; then
  systemctl --user stop netmon.service && restart=1
elif [ -f "$DIR/netmon.pid" ] && kill -0 "$(cat "$DIR/netmon.pid")" 2>/dev/null; then
  "$DIR/ctl.sh" stop >/dev/null; restart=2
fi

# Záloha (pokud není --purge)
if [ "$MODE" != "purge" ]; then
  STAMP=$(date +%Y%m%d-%H%M%S)
  ARCH="$DIR/archiv/$STAMP"
  mkdir -p "$ARCH"
  moved=0
  for f in "${LOGS[@]}"; do
    [ -f "$DIR/$f" ] && { mv "$DIR/$f" "$ARCH/"; moved=$((moved+1)); }
  done
  [ -f "$DIR/report.html" ] && cp "$DIR/report.html" "$ARCH/" 2>/dev/null
  echo "Zazálohováno $moved logů → $ARCH"
else
  for f in "${LOGS[@]}"; do rm -f "$DIR/$f"; done
  echo "Logy smazány (bez zálohy)."
fi

# Znovu spustit měření (čisté logy si skript vytvoří sám)
if [ "$restart" = 1 ]; then
  systemctl --user start netmon.service && echo "Měření znovu spuštěno (systemd)."
elif [ "$restart" = 2 ]; then
  "$DIR/ctl.sh" start
else
  echo "Hotovo. Měření spustíš: systemctl --user start netmon.service"
fi
