#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
root=$(pwd -P)
existing=/home/cassius/Videos/twins-paper-followthrough-20261122/project
target=/home/cassius/Videos/twins-paper-tcp-policy-20261122
lock=/home/cassius/Videos/twins-isolated-20261118/out/exclusive-campaign.lock
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
local_out="$root/out/review/tcp-policy-20261122/$stamp"
remote_out="$target/$stamp"
mkdir -p "$local_out"
"${ssh_cmd[@]}" "mkdir -p '$remote_out/experiments/paper_review'"
scp -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 \
    experiments/paper_review/tcp_wide_matrix.py "s50:$remote_out/experiments/paper_review/"
scp -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 \
    src/tcp_retry.py "s50:$remote_out/"
rc=0
"${ssh_cmd[@]}" "set -e
    cd '$remote_out'
    ln -s '$existing/experiments/paper_scale' experiments/paper_scale
    mkdir build
    cp '$existing'/build/twins* build/
    cp tcp_retry.py build/
    cp '$existing/revision' frozen-revision
    sha256sum experiments/paper_review/tcp_wide_matrix.py tcp_retry.py build/twins* > runner.sha256
    flock '$lock' timeout 90m python3 '$existing/experiments/paper_review/observe_cuda.py' observations \
         python3 experiments/paper_review/tcp_wide_matrix.py '$existing/data-input/tcp' \
             '$existing/data-input/ns3-101' '$existing/data-input/ns3-102' '$remote_out/build' \
             measurements --process-timing --all-scales --memory-backend=cuda --core 0" > "$local_out/worker.log" 2>&1 || rc=$?
printf '%s\n' "$rc" > "$local_out/remote.rc"
rsync -a --prune-empty-dirs -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
    --include='*/' --include='*.json' --include='*.log' --include='*.sha256' \
    --include='frozen-revision' --exclude='*' "s50:$remote_out/" "$local_out/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$remote_out' &&
    find . -type f \\( -name '*.json' -o -name '*.log' -o -name '*.sha256' -o -name 'frozen-revision' \\) \
    -print0 | sort -z | xargs -0 -r sha256sum" > "$local_out/returned-files.sha256"
test -s "$local_out/returned-files.sha256"
(cd "$local_out" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
