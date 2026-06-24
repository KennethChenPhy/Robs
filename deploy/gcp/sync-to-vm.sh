#!/usr/bin/env bash
# Run ON YOUR MAC. Upload trader code to GCP VM via tar+scp (reliable with gcloud).
#
# Usage:
#   ./deploy/gcp/sync-to-vm.sh [instance] [zone]
#   ./deploy/gcp/sync-to-vm.sh robs-trader asia-east2-a
#   ./deploy/gcp/sync-to-vm.sh --full robs-trader asia-east2-a
set -euo pipefail

INSTANCE="robs-trader"
ZONE="asia-east2-a"
FULL_SYNC=false

usage() {
  echo "Usage: $0 [--full] [instance] [zone]"
  echo "  e.g. $0 robs-trader asia-east2-a"
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) FULL_SYNC=true; shift ;;
    -h|--help) usage ;;
    -*)
      echo "Unknown option: $1"
      usage
      ;;
    *)
      INSTANCE="$1"
      shift
      if [[ $# -gt 0 && "$1" != --* ]]; then
        ZONE="$1"
        shift
      fi
      break
      ;;
  esac
done

if [[ -z "$INSTANCE" ]]; then
  echo "ERROR: instance name is required."
  usage
fi

ROBS_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
REMOTE_DIR="Robs"
ARCHIVE="$(mktemp "/tmp/robs-sync.XXXXXX.tar.gz")"
trap 'rm -f "$ARCHIVE"' EXIT

TAR_EXCLUDES=(
  --exclude='.git'
  --exclude='__pycache__'
  --exclude='*.pyc'
  --exclude='.DS_Store'
  --exclude='.env'
  --exclude='.venv'
  --exclude='deploy/gcp/.venv'
  --exclude='data'
  --exclude='*.parquet'
)

echo "Packing -> $INSTANCE ($ZONE) ~/$REMOTE_DIR"
echo "Mode: $([[ "$FULL_SYNC" == true ]] && echo full-repo || echo minimal-trader)"

if [[ "$FULL_SYNC" == true ]]; then
  tar czf "$ARCHIVE" "${TAR_EXCLUDES[@]}" -C "$ROBS_ROOT" .
else
  tar czf "$ARCHIVE" -C "$ROBS_ROOT" \
    robs \
    config \
    deploy \
    requirements.txt
fi

ARCHIVE_MB="$(du -m "$ARCHIVE" | cut -f1)"
echo "Archive size: ${ARCHIVE_MB} MB"

echo "Uploading..."
gcloud compute scp "$ARCHIVE" "${INSTANCE}:robs-sync.tar.gz" --zone="$ZONE"

echo "Extracting on VM..."
gcloud compute ssh "$INSTANCE" --zone="$ZONE" --command="
  set -e
  mkdir -p ~/${REMOTE_DIR}
  tar xzf ~/robs-sync.tar.gz -C ~/${REMOTE_DIR}
  rm -f ~/robs-sync.tar.gz
  chmod +x ~/${REMOTE_DIR}/deploy/gcp/*.sh 2>/dev/null || true
"

echo "Done."
echo "  gcloud compute ssh $INSTANCE --zone=$ZONE"
echo "  cd ~/$REMOTE_DIR && ./deploy/gcp/bootstrap-vm.sh   # first time only"
