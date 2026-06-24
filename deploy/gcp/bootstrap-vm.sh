#!/usr/bin/env bash
# Run ON the GCP Ubuntu VM (after SSH). Installs deps + Python venv for Robs.
set -euo pipefail

ROBS_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROBS_ROOT/deploy/gcp/.venv"

echo "==> Robs root: $ROBS_ROOT"

echo "==> System packages"
sudo apt-get update
sudo apt-get install -y \
  python3 python3-venv python3-pip \
  curl unzip ca-certificates \
  build-essential

# e2-micro has 1 GB RAM; swap helps OpenD + Python stay stable.
if ! swapon --show | grep -q '/swapfile'; then
  echo "==> Adding 2G swap (recommended on free-tier e2-micro)"
  sudo fallocate -l 2G /swapfile || sudo dd if=/dev/zero of=/swapfile bs=1M count=2048
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile
  sudo swapon /swapfile
  if ! grep -q '/swapfile' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  fi
fi

echo "==> Python venv"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --upgrade pip wheel
"$VENV/bin/pip" install -r "$ROBS_ROOT/deploy/gcp/requirements.txt"

chmod +x "$ROBS_ROOT/deploy/gcp/run-mhimain.sh"

echo ""
echo "Bootstrap done."
echo "Next:"
echo "  1) Install OpenD: see deploy/gcp/install-opend.sh"
echo "  2) First login:   deploy/gcp/opend-first-login.sh"
echo "  3) Test trader:   deploy/gcp/run-mhimain.sh --trend bull"
echo "  4) Enable systemd units in deploy/systemd/"
