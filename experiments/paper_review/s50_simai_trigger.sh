#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
stamp=$(date -u +%Y%m%dT%H%M%SZ)
remote="/home/cassius/Videos/twins-simai-trigger-20261122/$stamp"
dest="out/review/simai-trigger-20261122/$stamp"
mkdir -p "$dest"
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 "mkdir -p '$remote'"
scp -F /home/cassius/.ssh/config experiments/paper_review/simai_trigger_probe.cc experiments/paper_review/simai_trigger_probes.py "s50:$remote/"
rc=0
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 \
 "python3 '$remote/simai_trigger_probes.py' '$remote/results'" > "$dest/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a --include='*/' --include='*.json' --include='*.log' --include='*.h' --exclude='*' \
 -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' "s50:$remote/results/" "$dest/"
ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 s50 \
 "cd '$remote/results' && find . -type f \( -name '*.json' -o -name '*.log' -o -name '*.h' \) -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
test -s "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
