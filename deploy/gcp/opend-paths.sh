#!/usr/bin/env bash
# Resolve Futu OpenD install directory (supports versioned subfolders under /opt/futu-opend).
set -euo pipefail

OPEND_ROOT="${OPEND_ROOT:-/opt/futu-opend}"

find_opend_binary() {
  find "$OPEND_ROOT" -maxdepth 4 -type f \( -name OpenD -o -name FutuOpenD \) 2>/dev/null | head -1
}

resolve_opend_home() {
  if [[ -n "${OPEND_HOME:-}" && -d "$OPEND_HOME" ]]; then
    echo "$OPEND_HOME"
    return 0
  fi
  if [[ -L "$OPEND_ROOT/current" ]]; then
    readlink -f "$OPEND_ROOT/current"
    return 0
  fi
  local bin
  bin="$(find_opend_binary)"
  if [[ -n "$bin" ]]; then
    dirname "$bin"
    return 0
  fi
  echo "$OPEND_ROOT"
}

resolve_opend_xml() {
  local home="${1:-$(resolve_opend_home)}"
  local name
  for name in FutuOpenD.xml OpenD.xml; do
    if [[ -f "$home/$name" ]]; then
      echo "$home/$name"
      return 0
    fi
  done
  return 1
}

export_opend_paths() {
  local home xml
  home="$(resolve_opend_home)"
  xml="$(resolve_opend_xml "$home" || true)"
  echo "OPEND_HOME=$home"
  if [[ -n "$xml" ]]; then
    echo "OPEND_CFG_FILE=$xml"
  fi
}
