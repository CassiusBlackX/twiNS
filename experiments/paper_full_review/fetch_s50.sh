#!/usr/bin/env bash
set -euo pipefail
output=$(realpath "$1")
host=s50
root=/home/cassius/Videos/twins-full-review-20261126
mkdir -p "$output"
fetch() {
    local lane=$1
    local remote=$2
    local target="$output/$lane"
    local arrived=0
    for ((i=0; i<480; i++)); do
        if ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" "test -f '$root/$remote/exit-status'"; then
            arrived=1
            break
        fi
        sleep 30
    done
    test "$arrived" = 1
    mkdir -p "$target"
    rsync -a --partial --checksum -e 'ssh -o BatchMode=yes -o ConnectTimeout=8' "$host:$root/$remote/" "$target/"
    ssh -o BatchMode=yes -o ConnectTimeout=8 "$host" "set -o pipefail; cd '$root/$remote' && find . -type f -print0 | sort -z | xargs -0 -r sha256sum" >"$output/$lane.return.sha256"
    (cd "$target" && sha256sum --quiet -c "$output/$lane.return.sha256")
    printf '%s %s\n' "$lane" "$(cat "$target/exit-status")"
}
fetch cuda-rdma rdma-candidate/rdma-results >"$output/fetch-rdma.log" 2>&1 &
p1=$!
fetch cuda-tcp-v2 tcp-v2/results >"$output/fetch-tcp.log" 2>&1 &
p2=$!
fetch cuda-diagnostics cuda-diagnostics/results >"$output/fetch-diagnostics.log" 2>&1 &
p3=$!
status=0
for p in "$p1" "$p2" "$p3"; do wait "$p" || status=1; done
exit "$status"
