#!/usr/bin/env bash
# install-report-timer.sh — nainstaluje a zapne systemd user timer, který každý
# den ve 3:00 vygeneruje report za předchozí den (a pošle e-mail, je-li nastaven
# SMTP v .env). Odinstalace: ./install-report-timer.sh --uninstall
set -eu
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now netmon-report.timer 2>/dev/null || true
  rm -f "$UNIT_DIR/netmon-report.timer" "$UNIT_DIR/netmon-report.service"
  systemctl --user daemon-reload
  echo "Timer odinstalován."
  exit 0
fi

mkdir -p "$UNIT_DIR"
cp "$DIR/systemd/netmon-report.service" "$UNIT_DIR/"
cp "$DIR/systemd/netmon-report.timer"   "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now netmon-report.timer

echo "Hotovo. Timer běží."
echo
systemctl --user list-timers netmon-report.timer --no-pager || true
echo
echo "Ruční spuštění teď:   systemctl --user start netmon-report.service"
echo "Log posledního běhu:  journalctl --user -u netmon-report.service -n 50 --no-pager"
