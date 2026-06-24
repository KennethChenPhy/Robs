#!/usr/bin/env bash
# First-time OpenD login on the VM (interactive SMS). Run in tmux/SSH, not systemd yet.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="/etc/futu-opend.env"
EXAMPLE="$SCRIPT_DIR/futu-opend.env.example"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Create $ENV_FILE first:"
  echo "  ./deploy/gcp/configure-opend-paths.sh"
  echo "  sudo nano /etc/futu-opend.env   # set FUTU_ACCOUNT, FUTU_PASSWORD"
  exit 1
fi

if [[ -r "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
else
  # shellcheck disable=SC1090
  source <(sudo cat "$ENV_FILE")
fi

# shellcheck source=opend-paths.sh
source "$SCRIPT_DIR/opend-paths.sh"

: "${FUTU_ACCOUNT:?Set FUTU_ACCOUNT in $ENV_FILE}"
: "${FUTU_PASSWORD:?Set FUTU_PASSWORD in $ENV_FILE}"

OPEND_HOME="$(resolve_opend_home)"
OPEND_CFG_FILE="${OPEND_CFG_FILE:-$(resolve_opend_xml "$OPEND_HOME" 2>/dev/null || true)}"
LISTEN_IP="${LISTEN_IP:-127.0.0.1}"
LISTEN_PORT="${LISTEN_PORT:-11111}"

echo "OpenD home: $OPEND_HOME"
echo "Starting OpenD (SMS code may be required)..."
echo "When listening on $LISTEN_IP:$LISTEN_PORT, Ctrl+C then: ./deploy/gcp/install-systemd.sh"

cd "$OPEND_HOME"

CFG_ARGS=()
if [[ -n "$OPEND_CFG_FILE" && -f "$OPEND_CFG_FILE" ]]; then
  CFG_ARGS=(-cfg_file="$OPEND_CFG_FILE")
fi

if [[ -w "$OPEND_HOME" ]] && [[ -x "$(command -v futu-opend)" ]]; then
  exec futu-opend \
    -login_account="$FUTU_ACCOUNT" \
    -login_pwd="$FUTU_PASSWORD" \
    -lang=en \
    -api_ip="$LISTEN_IP" \
    -api_port="$LISTEN_PORT" \
    "${CFG_ARGS[@]}"
fi

exec sudo -E futu-opend \
  -login_account="$FUTU_ACCOUNT" \
  -login_pwd="$FUTU_PASSWORD" \
  -lang=en \
  -api_ip="$LISTEN_IP" \
  -api_port="$LISTEN_PORT" \
  "${CFG_ARGS[@]}"
