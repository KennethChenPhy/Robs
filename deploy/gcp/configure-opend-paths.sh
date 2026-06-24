#!/usr/bin/env bash
# Detect OpenD under /opt/futu-opend (e.g. Futu_OpenD_10.7.6728_Ubuntu18.04) and update /etc/futu-opend.env
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=opend-paths.sh
source "$SCRIPT_DIR/opend-paths.sh"

ENV_FILE="/etc/futu-opend.env"
EXAMPLE="$SCRIPT_DIR/futu-opend.env.example"

HOME_DIR="$(resolve_opend_home)"
XML_FILE="$(resolve_opend_xml "$HOME_DIR" || true)"
BIN="$(find_opend_binary)"

if [[ -z "$BIN" ]]; then
  echo "ERROR: no OpenD/FutuOpenD binary under /opt/futu-opend"
  echo "Install first: ./deploy/gcp/install-opend.sh ~/OpenD_*.tar.gz"
  exit 1
fi

echo "OpenD binary: $BIN"
echo "OpenD home:   $HOME_DIR"
echo "OpenD config: ${XML_FILE:-<none>}"

sudo mkdir -p "$OPEND_ROOT"
sudo ln -sfn "$HOME_DIR" "$OPEND_ROOT/current"
sudo chmod +x "$BIN"
sudo ln -sf "$BIN" /usr/local/bin/futu-opend

if [[ ! -f "$ENV_FILE" ]]; then
  sudo cp "$EXAMPLE" "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE — edit FUTU_ACCOUNT and FUTU_PASSWORD:"
  echo "  sudo nano $ENV_FILE"
fi

# Merge OPEND_HOME / OPEND_CFG_FILE into env (preserve credentials).
TMP="$(mktemp)"
sudo cat "$ENV_FILE" | grep -v '^OPEND_HOME=' | grep -v '^OPEND_CFG_FILE=' > "$TMP" || true
{
  cat "$TMP"
  echo "OPEND_HOME=$HOME_DIR"
  if [[ -n "$XML_FILE" ]]; then
    echo "OPEND_CFG_FILE=$XML_FILE"
  fi
} | sudo tee "$ENV_FILE" >/dev/null
rm -f "$TMP"
sudo chmod 600 "$ENV_FILE"

echo ""
echo "Updated $ENV_FILE"
echo "Symlink: $OPEND_ROOT/current -> $HOME_DIR"
echo "Next: ./deploy/gcp/opend-first-login.sh"
