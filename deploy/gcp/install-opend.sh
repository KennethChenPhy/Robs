#!/usr/bin/env bash
# Install official Futu OpenD (Linux CLI) under /opt/futu-opend.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=opend-paths.sh
source "$SCRIPT_DIR/opend-paths.sh"

INSTALL_DIR="/opt/futu-opend"
OPEND_TARBALL="${1:-}"

if [[ -z "$OPEND_TARBALL" || ! -f "$OPEND_TARBALL" ]]; then
  cat <<'EOF'
Usage: ./install-opend.sh /path/to/Futu_OpenD_*_Ubuntu*.tar.gz

Download OpenD for Ubuntu from Futu Open API docs, upload to VM, then run this script.
After install, run: ./deploy/gcp/configure-opend-paths.sh
EOF
  exit 1
fi

sudo mkdir -p "$INSTALL_DIR"
# Do not strip — Futu ships a versioned folder e.g. Futu_OpenD_10.7.6728_Ubuntu18.04/
sudo tar -xf "$OPEND_TARBALL" -C "$INSTALL_DIR"

"$SCRIPT_DIR/configure-opend-paths.sh"
