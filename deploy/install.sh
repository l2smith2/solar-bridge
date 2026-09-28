#!/bin/sh
# Install (or update) Solar Bridge on Raspberry Pi OS / Debian / Ubuntu:
#
#   curl -fsSL https://raw.githubusercontent.com/l2smith2/solar-bridge/main/deploy/install.sh | sudo sh
#
# Optional add-ons (can also be installed later from the settings page):
#   ... | sudo sh -s -- --tesla        Tesla Powerwall support
#   ... | sudo sh -s -- --yaml         YAML settings files (the web page doesn't need it)
set -eu
REPO="https://github.com/l2smith2/solar-bridge"
EXTRAS="app"
for arg in "$@"; do
  case "$arg" in
    --tesla) EXTRAS="$EXTRAS,tesla" ;;
    --yaml) EXTRAS="$EXTRAS,yaml" ;;
    *) echo "Unknown option: $arg" >&2; exit 1 ;;
  esac
done

echo "Installing Solar Bridge ($EXTRAS)…"
apt-get update -qq
apt-get install -y -qq python3-venv >/dev/null

python3 -m venv /opt/solar-bridge
/opt/solar-bridge/bin/pip install -q --upgrade pip
/opt/solar-bridge/bin/pip install -q --upgrade "solar-bridge[$EXTRAS] @ git+$REPO"

curl -fsSL "$REPO/raw/main/deploy/solar-bridge.service" -o /etc/systemd/system/solar-bridge.service
systemctl daemon-reload
systemctl enable solar-bridge >/dev/null
systemctl restart solar-bridge

echo
echo "Solar Bridge is running. Open this page to set it up:"
echo "    http://$(hostname).local/settings"
echo "(or http://$(hostname -I 2>/dev/null | cut -d' ' -f1)/settings)"
