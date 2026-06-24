#!/usr/bin/env bash
# Run MHImain trader on Linux (GCP VM). Uses deploy/gcp/.venv, not the Mac FTAPI bundle.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="$ROOT/deploy/gcp/.venv"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$VENV/bin/python" "$ROOT/robs/cli/mhimain.py" "$@"
