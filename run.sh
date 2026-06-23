#!/usr/bin/env bash
# Run a Robs script with the parent FTAPI venv.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/../bin/python" "$@"
