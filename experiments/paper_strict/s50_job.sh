#!/usr/bin/env bash
set -euo pipefail
# Keep a running job independent of edits to this launcher during a long run.
if [[ ${TWINS_STRICT_LAUNCHER_SNAPSHOT:-0} != 1 ]]; then
    snapshot_path=$(realpath -- "$0")
    launcher=$(< "$snapshot_path")
    exec env TWINS_STRICT_LAUNCHER_SNAPSHOT=1 /bin/bash -c "$launcher" "$snapshot_path" "$@"
fi
cd "$(dirname "$0")/../.."
root=$(pwd -P)
role=$1
stamp=$(date -u +%Y%m%dT%H%M%SZ)
periodic_filters=()
if [[ $role == lineage_cost ]]; then
    stamp="$stamp-${2:-baseline}-$$"
    periodic_filters=(--exclude='/runs/timed/**.fct' --exclude='/runs/timed/**.dag')
fi
remote="/home/cassius/Videos/twins-paper-strict-20261123/$role/$stamp"
dest="$root/out/strict-20261123/$role/$stamp"
ssh_cmd=(ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8 -o ServerAliveInterval=20 s50)
mkdir -p "$dest"
"${ssh_cmd[@]}" "mkdir -p '$remote'"
rsync -a -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
    --include='*.py' --exclude='*' experiments/paper_strict/ "s50:$remote/"
extra=()
case "$role" in
    crosspod_inputs|crosspod_paced)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes experiments/paper_review/days_pair_routes.rs "s50:$remote/"
        ;;
    pfc_reference|pfc_sustained|pfc_dense|mechanism_followup|pfc_bounded_reference)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes ../twiNS/workloads/trace2dag.py "s50:$remote/"
        ;;
    pfc_last_messages|pfc_wake_control)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes ../twiNS/workloads/trace2dag.py \
            experiments/paper_strict/pfc_last_messages.h "s50:$remote/"
        ;;
    lineage_cost)
        extra=( "${@:2}" )
        ;;
    rdma_memory)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes \
            out/followup-20261123/gpu_suite/20261122T164948Z/measurements/rdma-k32/summary.json "s50:$remote/frozen-rdma.json"
        extra=( "$remote/frozen-rdma.json" )
        ;;
    rdma_scale_reference|rdma_scale_route_control)
        scp -F /home/cassius/.ssh/config -o BatchMode=yes ../twiNS/workloads/topo.py "s50:$remote/"
        ;;
esac
"${ssh_cmd[@]}" "timeout 20h python3 '$remote/$role.py' '$remote/results' ${extra[*]}" > "$dest/worker.log" 2>&1 &
worker=$!
while kill -0 "$worker" 2>/dev/null; do
    rsync -a "${periodic_filters[@]}" --prune-empty-dirs --include='*/' --include='*.json' --include='*.jsonl' --include='*.log' \
        --include='*.fct' --include='*.csv' --include='*.dag' --include='*.toml' --include='*.graph' --include='*.nsys-rep' --exclude='*' \
        -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
        "s50:$remote/results/" "$dest/" 2>> "$dest/transfer.log" || true
    sleep 45
done
rc=0
wait "$worker" || rc=$?
printf '%s\n' "$rc" > "$dest/remote.rc"
rsync -a --checksum --prune-empty-dirs --include='*/' --include='*.json' --include='*.jsonl' --include='*.log' \
    --include='*.fct' --include='*.csv' --include='*.dag' --include='*.toml' --include='*.graph' --include='*.nsys-rep' --exclude='*' \
    -e 'ssh -F /home/cassius/.ssh/config -o BatchMode=yes -o ConnectTimeout=8' \
    "s50:$remote/results/" "$dest/"
"${ssh_cmd[@]}" "set -o pipefail; cd '$remote/results' &&
    find . -type f \( -name '*.json' -o -name '*.jsonl' -o -name '*.log' -o -name '*.fct' -o -name '*.csv' -o -name '*.dag' -o -name '*.toml' -o -name '*.graph' -o -name '*.nsys-rep' \) \
    -print0 | sort -z | xargs -0 -r sha256sum" > "$dest/returned-files.sha256"
test -s "$dest/returned-files.sha256"
(cd "$dest" && sha256sum --quiet -c returned-files.sha256)
exit "$rc"
