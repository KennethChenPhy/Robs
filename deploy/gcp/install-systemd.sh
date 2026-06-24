#!/usr/bin/env bash
# Install systemd units on the VM. Replace USER below with your SSH username.
set -euo pipefail

USER_NAME="${1:-$(whoami)}"
ROBS_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

sudo cp "$ROBS_ROOT/deploy/systemd/futu-opend.service" /etc/systemd/system/
sed "s|%i|$USER_NAME|g; s|%h|/home/$USER_NAME|g" \
  "$ROBS_ROOT/deploy/systemd/robs-mhimain.service" \
  | sudo tee /etc/systemd/system/robs-mhimain.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable futu-opend.service robs-mhimain.service

echo "Enabled. Start with:"
echo "  sudo systemctl start futu-opend"
echo "  sudo systemctl start robs-mhimain"
echo "Logs:"
echo "  journalctl -u futu-opend -f"
echo "  journalctl -u robs-mhimain -f"
