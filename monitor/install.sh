#!/usr/bin/env bash
#
# install.sh — installs the netmon monitor as a systemd user service.
# Usage:  ./install.sh              # install + start
#         ./install.sh --uninstall
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
  echo "Uninstalled. Data in ~/.local/share/netmon/ was kept."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is missing."; exit 1; }

mkdir -p "$CONF_DIR" "$UNIT_DIR"
if [ ! -f "$CONF_DIR/monitor.ini" ]; then
  cp "$DIR/monitor.ini.example" "$CONF_DIR/monitor.ini"
  echo "Created $CONF_DIR/monitor.ini — EDIT at least 'network' and 'token'!"
fi

# Unit with the absolute path to this directory (may not be ~/netmon/monitor)
sed -e "s|%h/netmon/monitor|$DIR|g" "$DIR/systemd/$UNIT" > "$UNIT_DIR/$UNIT"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Done. Status:    systemctl --user status $UNIT"
echo "Log:             journalctl --user -u $UNIT -f"
echo "Config:          $CONF_DIR/monitor.ini (after editing: systemctl --user restart $UNIT)"
echo
echo "To keep the service running without a login session (after reboot):"
echo "  loginctl enable-linger $USER"
