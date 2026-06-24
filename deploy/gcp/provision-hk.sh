#!/usr/bin/env bash
# Run ON YOUR MAC. Create a Hong Kong VM for the 90-day GCP free trial.
# Region: asia-east2 (Hong Kong) — best latency for HK.MHI futures.
set -euo pipefail

PROJECT="${GCP_PROJECT:-}"
INSTANCE="${INSTANCE:-robs-trader}"
ZONE="${ZONE:-asia-east2-a}"
MACHINE="${MACHINE:-e2-small}"   # 2 vCPU, 2 GB — comfortable for OpenD + trader (trial credits)

if [[ -z "$PROJECT" ]]; then
  echo "Set GCP project name, e.g.:"
  echo "  export GCP_PROJECT=robs-trading"
  echo "  ./deploy/gcp/provision-hk.sh"
  exit 1
fi

gcloud config set project "$PROJECT"

gcloud services enable compute.googleapis.com

# Create VM (trial credits apply; not the always-free US e2-micro tier)
gcloud compute instances create "$INSTANCE" \
  --zone="$ZONE" \
  --machine-type="$MACHINE" \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=30GB \
  --tags=ssh-only

# SSH only — never expose OpenD port 11111
if ! gcloud compute firewall-rules describe allow-ssh-robs &>/dev/null; then
  gcloud compute firewall-rules create allow-ssh-robs \
    --allow=tcp:22 \
    --target-tags=ssh-only \
    --description="SSH for robs-trader VM"
fi

echo ""
echo "VM ready: $INSTANCE in $ZONE (Hong Kong)"
echo ""
echo "Next:"
echo "  gcloud compute ssh $INSTANCE --zone=$ZONE"
echo "  # On Mac, sync code:"
echo "  ./deploy/gcp/sync-to-vm.sh $INSTANCE $ZONE"
