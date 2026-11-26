#!/usr/bin/env bash
set -euo pipefail
old=/home/cassius/Videos/twins-isolated-20261118/out
test -s "$old/export/all-finished.json"
exec 9>"$old/exclusive-campaign.lock"
flock 9
while [[ -n $(nvidia-smi --query-compute-apps=pid --format=csv,noheader) ]]; do sleep 5; done
base=/home/cassius/Videos/twins-full-review-20261126
sha256sum "$base/tcp-v2/binaries/twins" "$base/tcp-v2/binaries/twins2048-options-links" > original-binaries.sha256
python3 pid_memory.py "$base/tcp-v2/binaries" "$base/tcp-inputs" original-memory --expected "$(cat memory.expected)"
