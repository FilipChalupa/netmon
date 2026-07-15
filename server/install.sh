#!/usr/bin/env bash
#
# install.sh — installs the netmon evaluation server as a systemd service
# (the no-Docker path, e.g. a Proxmox LXC container).
#
#   sudo ./install.sh              # install + start (creates venv, config, service)
#   sudo ./install.sh --uninstall  # remove the service (keeps config + data)
#
# Runs as a dedicated 'netmon' user; config in /etc/netmon/ (server.env,
# monitors.toml), database in /var/lib/netmon-server/.
#
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="netmon-server.service"

[ "$(id -u)" = 0 ] || { echo "This install needs root: sudo ./install.sh"; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now "$UNIT" 2>/dev/null || true
  rm -f "/etc/systemd/system/$UNIT"
  systemctl daemon-reload
  echo "Uninstalled. Config in /etc/netmon/ and data in /var/lib/netmon-server/ were kept."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is missing (apt install python3)."; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "venv is missing (apt install python3-venv)."; exit 1; }

# the service runs as the 'netmon' user, which cannot traverse /root or /home/<user>
case "$DIR" in
  /root/*|/home/*)
    REPO_ROOT="$(dirname "$DIR")"
    echo "ERROR: $DIR is not accessible to the 'netmon' service user (it lives under a private home directory)."
    echo "Move the checkout somewhere world-readable and re-run, e.g.:"
    echo "  mv $REPO_ROOT /opt/netmon && cd /opt/netmon/server && sudo ./install.sh"
    exit 1 ;;
esac

id netmon >/dev/null 2>&1 || \
  useradd --system --home-dir /var/lib/netmon-server --shell /usr/sbin/nologin netmon

mkdir -p /etc/netmon /var/lib/netmon-server
chown netmon:netmon /var/lib/netmon-server

[ -f /etc/netmon/server.env ] || {
  cp "$DIR/server.env.example" /etc/netmon/server.env
  echo "Created /etc/netmon/server.env — fill in SMTP_* if you want reports/alerts."
}
[ -f /etc/netmon/monitors.toml ] || {
  cp "$DIR/config/monitors.toml.example" /etc/netmon/monitors.toml
  echo "Created /etc/netmon/monitors.toml — EDIT the monitor URLs and tokens!"
}

echo "Creating venv and installing dependencies…"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"
chmod -R a+rX "$DIR"

sed -e "s|/opt/netmon/server|$DIR|g" \
  "$DIR/systemd/$UNIT" > "/etc/systemd/system/$UNIT"

systemctl daemon-reload
systemctl enable --now "$UNIT"

echo
echo "Done. Web UI:    http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
echo "Status:          systemctl status $UNIT"
echo "Log:             journalctl -u $UNIT -f"
echo "Monitors:        /etc/netmon/monitors.toml   (then: systemctl restart $UNIT)"
echo "Env/SMTP:        /etc/netmon/server.env      (then: systemctl restart $UNIT)"
echo
echo "Import of historical CSVs (per network):"
echo "  sudo -u netmon $DIR/.venv/bin/python -m netmon_server.importer \\"
echo "    --network home --label \"Home\" /path/to/log   # run inside $DIR"
