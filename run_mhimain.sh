#!/usr/bin/env bash
# Run the dedicated HK.MHImain trader (parent FTAPI venv).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/../bin/python" "$ROOT/robs/cli/mhimain.py" "$@"
