#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=20 s50)
while systemctl --user show twins-paper-review-cuda.service twins-paper-review-followthrough.service twins-paper-gpu-pair.service \
        -p MainPID --value | rg -q '^[1-9][0-9]*$'; do
    sleep 30
done
stamp=$(date -u +%Y%m%dT%H%M%SZ)
target="/home/cassius/Videos/twins-simai-reference-20261122/$stamp"
dest="$root/out/review/simai-reference-20261122/$stamp"
mkdir -p "$dest"
"${ssh_cmd[@]}" "mkdir -p '$target'"
scp -F /home/cassius/.ssh/config experiments/paper_review/simai_reference_study.py validation/simai/trace_patch.py "s50:$target/"
cd "$root"
rc=0
"${ssh_cmd[@]}" "flock /home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock \
    timeout 8h python3 '$target/simai_reference_study.py' '$target/work' '$target/trace_patch.py'" > "$dest/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a --checksum -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=10' "s50:$target/work/results/" "$dest/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$target/work/results' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
test -s "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
