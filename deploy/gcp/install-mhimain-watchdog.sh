#!/usr/bin/env bash
# Install cron watchdog: ntfy push if mhimain.py is not running.
# Run on the GCP VM as the user that runs the trader (usually CK).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
ENV_DIR="$HOME/.config/robs"
ENV_FILE="$ENV_DIR/watchdog.env"
EXAMPLE="$ROOT/deploy/gcp/watchdog.env.example"
CHECK="$ROOT/deploy/gcp/check-mhimain.sh"
CRON_TZ_LINE="CRON_TZ=Asia/Hong_Kong"
CRON_LINE="0 * * * 1-5 $CHECK >> $HOME/Robs/logs/watchdog.log 2>&1"
WATCHDOG_MARK="# robs-mhimain-watchdog (Asia/Hong_Kong)"

chmod +x "$CHECK"

echo "Setting VM timezone to Asia/Hong_Kong..."
sudo timedatectl set-timezone Asia/Hong_Kong
timedatectl | sed -n '1,3p'

mkdir -p "$ENV_DIR" "$HOME/Robs/logs"
if [[ ! -f "$ENV_FILE" ]]; then
  cp "$EXAMPLE" "$ENV_FILE"
  TOPIC="robs-mhimain-$(openssl rand -hex 4)"
  sed -i "s/robs-mhimain-CHANGE_ME/${TOPIC}/" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE with topic: $TOPIC"
  echo ""
  echo "Subscribe on your phone (ntfy app or https://ntfy.sh):"
  echo "  Topic: $TOPIC"
  echo ""
  echo "Then re-run: $0"
  exit 0
fi
chmod 600 "$ENV_FILE"

if grep -q 'CHANGE_ME' "$ENV_FILE" 2>/dev/null; then
  TOPIC="robs-mhimain-$(openssl rand -hex 4)"
  sed -i "s/robs-mhimain-CHANGE_ME/${TOPIC}/" "$ENV_FILE"
  echo "Generated topic: $TOPIC"
  echo "Subscribe in ntfy app, then re-run: $0"
  exit 0
fi

# shellcheck disable=SC1090
source "$ENV_FILE"
if [[ -z "${WATCHDOG_NTFY_TOPIC:-}" ]]; then
  echo "Set WATCHDOG_NTFY_TOPIC in $ENV_FILE"
  exit 1
fi

TMP="$(mktemp)"
crontab -l 2>/dev/null \
  | grep -v 'check-mhimain.sh' \
  | grep -v '^CRON_TZ=Asia/Hong_Kong' \
  | grep -v '^# robs-mhimain-watchdog' \
  >"$TMP" || true
{
  echo "$WATCHDOG_MARK"
  echo "$CRON_TZ_LINE"
  echo "$CRON_LINE"
} >>"$TMP"
crontab "$TMP"
rm -f "$TMP"

echo "Installed cron (hourly Mon–Fri HKT, at :00):"
echo "  $CRON_TZ_LINE"
echo "  $CRON_LINE"
echo ""
echo "ntfy topic: ${WATCHDOG_NTFY_TOPIC}"
echo "Subscribe: ntfy app → + → Topic subscription → ${WATCHDOG_NTFY_TOPIC}"
echo "  or open: ${WATCHDOG_NTFY_SERVER:-https://ntfy.sh}/${WATCHDOG_NTFY_TOPIC}"
echo ""
echo "Test notification:"
curl -fsS -X POST \
  -H "Title: Robs watchdog test" \
  -d "check-mhimain watchdog installed on $(hostname -s)" \
  "${WATCHDOG_NTFY_SERVER:-https://ntfy.sh}/${WATCHDOG_NTFY_TOPIC}"
echo ""
echo "Test DOWN check (trader running → no alert):"
"$CHECK"
