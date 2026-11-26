#!/usr/bin/env bash
set -euo pipefail
binary=$(realpath "$1")
inputs=$(realpath "$2")
output=$(realpath -m "$3")
mkdir "$output"
run() {
    local pool=$1
    local arm=$2
    shift 2
    local input="$inputs/pool-$pool/input"
    local result="$output/$pool/$arm"
    mkdir -p "$result"
    (
        cd "$result"
        rc=0
        timeout 120s "$binary" "$input/config.log" "--topo=$input/topo.log" "--astra=$input/workload.log" \
          --out=messages.fct --dag-out=actions.dag --nvl-islands=0 --gwin=0 --fp-bases=1000003,1000033 "$@" >run.log 2>&1 || rc=$?
        printf '%s\n' "$rc" >rc
    )
}
for pool in 06 64; do
    run "$pool" checked
    run "$pool" serial --serial
done
run 06 tail --tail=1
run 64 cache "--flows-cache=$output/flows.dag"
run 06 cache "--flows-cache=$output/flows.dag"
