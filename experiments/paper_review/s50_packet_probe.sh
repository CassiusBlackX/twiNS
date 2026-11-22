#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
dest="$root/out/review/days-packets-20261122/$stamp"
remote="/home/cassius/Videos/twins-days-probe-20261122/results/$stamp"
mkdir -p "$dest"
rc=0
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50 "
    mkdir -p '$remote'
    cd /home/cassius/Videos/twins-days-probe-20261122
    sha256sum days-packet-probe days_packet_probe.rs > '$remote/observer.sha256'
    flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock \
      timeout 30m python3 /home/cassius/Videos/twins-paper-followthrough-20261122/project/experiments/paper_review/observe_cuda.py \
        '$remote' bash -c '
          for k in 8 16; do
            taskset -c 2 ./days-packet-probe /home/cassius/Videos/twins-paper-pair-20261122/project/common-inputs/k\$k/days.toml \
               > $remote/k\$k.csv 2> $remote/k\$k.log || exit
          done'" > "$dest/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' "s50:$remote/" "$dest/"
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 \
    "cd '$remote' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
