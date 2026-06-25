#!/usr/bin/env bash
# Run MHImain trader on Linux (GCP VM). Uses deploy/gcp/.venv, not the Mac FTAPI bundle.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/deploy/gcp/.venv"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

# One trader process per VM — systemd and tmux/manual runs share this lock.
LOCKFILE="${ROBS_MHIMAIN_LOCK:-/tmp/robs-mhimain.lock}"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "ERROR: another mhimain is already running (lock: $LOCKFILE)." >&2
  echo "Use only ONE of: systemd (robs-mhimain) OR a manual/tmux run — not both." >&2
  echo "  pgrep -af mhimain.py" >&2
  echo "  sudo systemctl stop robs-mhimain   # if using tmux/manual only" >&2
  exit 1
fi

exec "$VENV/bin/python" "$ROOT/robs/cli/mhimain.py" "$@"
