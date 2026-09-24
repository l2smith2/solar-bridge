#!/bin/sh
# Install Solar Bridge on Raspberry Pi OS / Debian:
#   curl -fsSL https://raw.githubusercontent.com/l2smith2/solar-bridge/main/deploy/install.sh | sudo sh
set -eu
REPO="https://github.com/l2smith2/solar-bridge"

apt-get update -qq
apt-get install -y -qq python3-venv >/dev/null

python3 -m venv /opt/solar-bridge
/opt/solar-bridge/bin/pip install -q --upgrade pip
/opt/solar-bridge/bin/pip install -q "solar-bridge[app] @ git+$REPO"

mkdir -p /etc/solar-bridge
if [ ! -f /etc/solar-bridge/config.yaml ]; then
  curl -fsSL "$REPO/raw/main/deploy/config.example.yaml" -o /etc/solar-bridge/config.yaml
fi
curl -fsSL "$REPO/raw/main/deploy/solar-bridge.service" -o /etc/systemd/system/solar-bridge.service
systemctl daemon-reload
systemctl enable solar-bridge >/dev/null

echo
echo "Installed. Next:"
echo "  1. sudo nano /etc/solar-bridge/config.yaml"
echo "  2. sudo /opt/solar-bridge/bin/solar-bridge --check"
echo "  3. sudo systemctl start solar-bridge"
echo "  4. open http://$(hostname).local/"
