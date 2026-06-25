#!/usr/bin/env bash
# Alert via ntfy if mhimain.py is not running. Install: ./deploy/gcp/install-mhimain-watchdog.sh
set -euo pipefail

ENV_FILE="${ROBS_WATCHDOG_ENV:-$HOME/.config/robs/watchdog.env}"
STATE_FILE="${ROBS_WATCHDOG_STATE:-$HOME/Robs/logs/.watchdog-state}"
LOG_TAG="robs-watchdog"
_now() { date '+%Y-%m-%d %H:%M:%S'; }

mkdir -p "$(dirname "$STATE_FILE")" "$(dirname "$ENV_FILE")" 2>/dev/null || true

_ntfy_notify() {
  local title="$1"
  local message="$2"
  local priority="${3:-default}"

  local server="${WATCHDOG_NTFY_SERVER:-https://ntfy.sh}"
  local topic="${WATCHDOG_NTFY_TOPIC:?WATCHDOG_NTFY_TOPIC not set}"
  local url="${server%/}/${topic}"

  local -a curl_args=(
    -fsS
    -X POST
    -H "Title: ${title}"
    -H "Priority: ${priority}"
    -H "Tags: chart_with_downwards_trend"
    -d "$message"
  )
  if [[ -n "${WATCHDOG_NTFY_TOKEN:-}" ]]; then
    curl_args+=(-H "Authorization: Bearer ${WATCHDOG_NTFY_TOKEN}")
  fi

  curl "${curl_args[@]}" "$url"
  echo "[$LOG_TAG] ntfy sent: $title"
}

if pgrep -f 'robs/cli/mhimain\.py' >/dev/null 2>&1; then
  if [[ -f "$STATE_FILE" ]] && grep -q '^alerted_down=1' "$STATE_FILE" 2>/dev/null; then
    echo "$(_now) [$LOG_TAG] trader recovered"
    if [[ -f "$ENV_FILE" ]]; then
      # shellcheck disable=SC1090
      source "$ENV_FILE"
      host="$(hostname -s)"
      _ntfy_notify \
        "Robs mhimain RECOVERED ($host)" \
        "mhimain.py is running again on $(hostname -f) at $(_now).

$(pgrep -af 'robs/cli/mhimain\.py' || true)" \
        "default" || true
    fi
  fi
  echo "alerted_down=0" >"$STATE_FILE"
  exit 0
fi

echo "$(_now) [$LOG_TAG] TRADER DOWN"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[$LOG_TAG] missing $ENV_FILE — run ./deploy/gcp/install-mhimain-watchdog.sh" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

COOLDOWN_MIN="${WATCHDOG_COOLDOWN_MIN:-60}"
now_epoch="$(date +%s)"
last_epoch=0
if [[ -f "$STATE_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE" 2>/dev/null || true
  last_epoch="${last_alert_epoch:-0}"
fi

if [[ "${alerted_down:-0}" == "1" ]]; then
  elapsed=$(( now_epoch - last_epoch ))
  if (( elapsed < COOLDOWN_MIN * 60 )); then
    echo "[$LOG_TAG] still down; next alert in $(( COOLDOWN_MIN * 60 - elapsed ))s"
    exit 0
  fi
fi

host="$(hostname -s)"
_ntfy_notify \
  "Robs mhimain DOWN ($host)" \
  "mhimain.py is not running on $(hostname -f) at $(_now).

Check:
  pgrep -af mhimain.py
  tmux ls
  journalctl -u futu-opend -n 20 --no-pager

Restart (REAL manual):
  sudo systemctl stop robs-mhimain
  tmux new -s mhimain
  cd ~/Robs && ./deploy/gcp/run-mhimain.sh --trend uncertain" \
  "urgent"

echo "alerted_down=1" >"$STATE_FILE"
echo "last_alert_epoch=$now_epoch" >>"$STATE_FILE"
