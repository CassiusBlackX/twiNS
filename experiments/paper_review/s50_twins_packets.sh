#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
stamp=$(date -u +%Y%m%dT%H%M%SZ)
remote="/home/cassius/Videos/twins-tcp-packets-20261122/$stamp"
dest="out/review/twins-packets-20261122/$stamp"
mkdir -p "$dest"
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 "mkdir -p '$remote'"
scp -F /home/cassius/.ssh/config experiments/paper_review/twins_packet_probe.py "s50:$remote/"
rc=0
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50 \
 "flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock timeout 30m \
  python3 /home/cassius/Videos/twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py \
  '$remote' python3 '$remote/twins_packet_probe.py' '$remote/results'" > "$dest/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a --include='*/' --include='*.json' --include='*.log' --include='*.csv' --exclude='*' \
 -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' "s50:$remote/results/" "$dest/"
scp -F /home/cassius/.ssh/config "s50:$remote/gpu-ownership.json" "$dest/" || true
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 \
 "cd '$remote/results' && find . -type f \( -name '*.json' -o -name '*.log' -o -name '*.csv' \) \
 -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
test -s "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
