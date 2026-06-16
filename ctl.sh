#!/usr/bin/env bash
# ctl.sh — start/stop/status pro netmon.sh (běží na pozadí, přežije zavření terminálu)
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDFILE="$DIR/netmon.pid"
RUNLOG="$DIR/netmon.run.log"

is_running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if is_running; then echo "Už běží (PID $(cat "$PIDFILE"))."; exit 0; fi
    nohup bash "$DIR/netmon.sh" >>"$RUNLOG" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Spuštěno (PID $!). Logy: $DIR/latency.csv, $DIR/speed.csv"
    ;;
  stop)
    if is_running; then
      kill -TERM "$(cat "$PIDFILE")"; sleep 1; rm -f "$PIDFILE"
      echo "Zastaveno."
    else
      echo "Neběží."; rm -f "$PIDFILE"
    fi
    ;;
  status)
    if is_running; then
      echo "Běží (PID $(cat "$PIDFILE"))."
      echo "  latency.csv: $(($(wc -l <"$DIR/latency.csv")-1)) záznamů"
      echo "  speed.csv:   $(($(wc -l <"$DIR/speed.csv")-1)) měření"
    else
      echo "Neběží."
    fi
    ;;
  *)
    echo "Použití: $0 {start|stop|status}"; exit 1;;
esac
