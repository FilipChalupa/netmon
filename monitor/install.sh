#!/usr/bin/env bash
#
# install.sh — nainstaluje netmon monitor jako systemd user službu.
# Použití:  ./install.sh            # instalace + start
#           ./install.sh --uninstall
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/netmon"
UNIT="netmon-monitor.service"

if [ "${1:-}" = "--uninstall" ]; then
  systemctl --user disable --now "$UNIT" 2>/dev/null || true
  rm -f "$UNIT_DIR/$UNIT"
  systemctl --user daemon-reload
  echo "Odinstalováno. Data v ~/.local/share/netmon/ zůstala."
  exit 0
fi

command -v python3 >/dev/null || { echo "Chybí python3."; exit 1; }

mkdir -p "$CONF_DIR" "$UNIT_DIR"
if [ ! -f "$CONF_DIR/monitor.ini" ]; then
  cp "$DIR/monitor.ini.example" "$CONF_DIR/monitor.ini"
  echo "Vytvořen $CONF_DIR/monitor.ini — UPRAV alespoň 'network' a 'token'!"
fi

# Unit s absolutní cestou k této složce (nemusí být zrovna ~/netmon/monitor)
sed -e "s|%h/netmon/monitor|$DIR|g" "$DIR/systemd/$UNIT" > "$UNIT_DIR/$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Hotovo. Stav:    systemctl --user status $UNIT"
echo "Log:             journalctl --user -u $UNIT -f"
echo "Konfigurace:     $CONF_DIR/monitor.ini (po úpravě: systemctl --user restart $UNIT)"
echo
echo "Aby služba běžela i bez přihlášení (po rebootu):"
echo "  loginctl enable-linger $USER"
