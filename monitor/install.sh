#!/usr/bin/env bash
#
# install.sh — installs the netmon monitor as a systemd service.
#
# User service (default; measurement runs under your user):
#   ./install.sh
#   ./install.sh --uninstall
#
# System service (for LXC containers / headless servers; runs as a dedicated
# 'netmon' user, config in /etc/netmon, data in /var/lib/netmon):
#   sudo ./install.sh --system
#   sudo ./install.sh --system --uninstall
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="netmon-monitor.service"

SYSTEM=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --system) SYSTEM=1 ;;
    --uninstall) UNINSTALL=1 ;;
    *) echo "Unknown option: $arg"; exit 1 ;;
  esac
done

# ---------------------------------------------------------------- system-wide
if [ "$SYSTEM" = 1 ]; then
  [ "$(id -u)" = 0 ] || { echo "System install needs root: sudo ./install.sh --system"; exit 1; }

  if [ "$UNINSTALL" = 1 ]; then
    systemctl disable --now "$UNIT" 2>/dev/null || true
    rm -f "/etc/systemd/system/$UNIT"
    systemctl daemon-reload
    echo "Uninstalled. Config in /etc/netmon/ and data in /var/lib/netmon/ were kept."
    exit 0
  fi

  command -v python3 >/dev/null || { echo "python3 is missing."; exit 1; }
  command -v ping >/dev/null || { echo "ping is missing (apt install iputils-ping)."; exit 1; }

  id netmon >/dev/null 2>&1 || \
    useradd --system --home-dir /var/lib/netmon --shell /usr/sbin/nologin netmon

  mkdir -p /etc/netmon /var/lib/netmon
  chown netmon:netmon /var/lib/netmon

  if [ ! -f /etc/netmon/monitor.ini ]; then
    # same example config, but the database belongs to /var/lib/netmon
    sed 's|^db_path *=.*|db_path = /var/lib/netmon/monitor.db|' \
      "$DIR/monitor.ini.example" > /etc/netmon/monitor.ini
    echo "Created /etc/netmon/monitor.ini — EDIT at least 'network' and 'token'!"
  fi

  # the 'netmon' user must be able to read the code
  chmod -R a+rX "$DIR"

  sed -e "s|/opt/netmon/monitor|$DIR|g" \
    "$DIR/systemd/netmon-monitor-system.service" > "/etc/systemd/system/$UNIT"

  systemctl daemon-reload
  systemctl enable --now "$UNIT"

  echo
  echo "Done. Status:    systemctl status $UNIT"
  echo "Log:             journalctl -u $UNIT -f"
  echo "Config:          /etc/netmon/monitor.ini (after editing: systemctl restart $UNIT)"
  exit 0
fi

# ----------------------------------------------------------------- user-level
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/netmon"

if [ "$UNINSTALL" = 1 ]; then
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
